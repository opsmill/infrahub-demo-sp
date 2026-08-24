"""Host-architecture guard and image resolution used by ``invoke lab.deploy``.

cEOS-lab has a 32-bit x86 userland in every build Arista publishes, so a 64-bit
ARM host cannot run it under any tag. Getting this wrong is expensive: it does
not fail at deploy, it fails seventeen minutes later as twelve routers exiting
255 behind a containerlab spinner that says only "Running postdeploy actions".
These tests pin the refusal, because the whole point of it is to be loud.
"""

from __future__ import annotations

import pytest
from invoke.exceptions import Exit

from tasks import (
    CEOS_IMAGE_DEFAULT,
    LAB_DEPLOY_TIMEOUT_SECONDS,
    _assert_ceos_runnable,
    _ceos_arch_blocker,
    _report_stalled_deploy,
    _resolve_ceos_image,
)

ARM64_MACHINES = ["aarch64", "arm64", "ARM64", "armv8l"]
X86_MACHINES = ["x86_64", "amd64", "AMD64"]


@pytest.mark.parametrize("machine", ARM64_MACHINES)
def test_arm64_hosts_are_reported_as_unable_to_run_ceos(machine: str) -> None:
    """Every 64-bit ARM spelling is refused.

    ``aarch64`` is what Apple Silicon reports through a Linux VM (OrbStack,
    Lima, UTM), which is the case that cost a customer two rounds of debugging,
    so the spellings are covered explicitly rather than assumed.
    """
    blocker = _ceos_arch_blocker(machine)
    assert blocker is not None
    assert machine in blocker


@pytest.mark.parametrize("machine", X86_MACHINES)
def test_x86_hosts_are_not_blocked(machine: str) -> None:
    """Intel/AMD hosts are the supported case and pass through."""
    assert _ceos_arch_blocker(machine) is None


def test_an_unknown_architecture_is_not_blocked() -> None:
    """An unrecognised architecture is allowed through to fail on its own.

    Only ARM is known to be unable to run the image. Anything else fails
    legibly at ``docker pull`` or at boot, which beats refusing a host that
    might work for a reason this function cannot know.
    """
    assert _ceos_arch_blocker("riscv64") is None


def test_the_blocker_names_the_supported_alternative() -> None:
    """The refusal has to tell the user where the lab does run.

    A refusal that only says "no" sends people back to Slack, which is exactly
    the loop this guard exists to break.
    """
    blocker = _ceos_arch_blocker("aarch64")
    assert blocker is not None
    assert "x86_64" in blocker
    assert "INFRAHUB_DATASET=isp" in blocker
    assert "LAB_ALLOW_UNSUPPORTED_ARCH" in blocker


@pytest.mark.parametrize("machine", ARM64_MACHINES)
def test_deploy_is_refused_on_arm64(machine: str) -> None:
    """The guard exits non-zero rather than letting the deploy start."""
    with pytest.raises(Exit) as excinfo:
        _assert_ceos_runnable(machine, allow_override=False)
    assert excinfo.value.code == 1


@pytest.mark.parametrize("machine", X86_MACHINES)
def test_deploy_proceeds_on_x86(machine: str) -> None:
    """A supported host raises nothing."""
    _assert_ceos_runnable(machine, allow_override=False)


def test_the_override_downgrades_the_refusal_to_a_warning() -> None:
    """``LAB_ALLOW_UNSUPPORTED_ARCH`` lets someone try it anyway.

    Kept deliberately: the finding behind this guard came from running it and
    reading the logs, and an ARM cEOS-lab build could appear later.
    """
    _assert_ceos_runnable("aarch64", allow_override=True)


def test_the_pinned_image_is_cgroup_v2_capable() -> None:
    """The pin does not predate 4.32.0F.

    cEOS-lab builds older than 4.32.0F require a cgroups v1 host and never
    finish booting on a cgroups v2 one — the default on Ubuntu 21.04+, OrbStack
    and most current distros. The failure is silent, so it is worth a test
    rather than a comment.
    """
    major, minor = (int(part) for part in CEOS_IMAGE_DEFAULT.rsplit(":", 1)[1].split(".")[:2])
    assert (major, minor) >= (4, 32)


def test_an_exported_ceos_image_wins_over_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CEOS_IMAGE`` from the environment overrides the pin.

    ``lab.deploy`` passes the resolved image through ``c.run(env=...)``, which
    merges over ``os.environ`` — so resolving to the default unconditionally
    would silently discard a build the user pinned. That path is the documented
    escape hatch for an Arista-supplied build and for re-testing the LDP
    data-plane gap, so it has to hold.
    """
    monkeypatch.setenv("CEOS_IMAGE", "ceos:local")
    assert _resolve_ceos_image() == "ceos:local"


def test_no_exported_ceos_image_falls_back_to_the_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing exported, the image is the single pinned one."""
    monkeypatch.delenv("CEOS_IMAGE", raising=False)
    assert _resolve_ceos_image() == CEOS_IMAGE_DEFAULT


def test_an_empty_ceos_image_falls_back_to_the_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``CEOS_IMAGE`` is treated as unset rather than passed through.

    containerlab's ``${VAR:=default}`` expansion does the same, so an empty
    export would otherwise leave the topology and the environment disagreeing.
    """
    monkeypatch.setenv("CEOS_IMAGE", "")
    assert _resolve_ceos_image() == CEOS_IMAGE_DEFAULT


class _RecordingContext:
    """Minimal Invoke-context stand-in that records the commands it is given."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, **_kwargs: object) -> None:
        """Record a command instead of running it."""
        self.commands.append(command)


def test_a_stalled_deploy_reports_per_node_state() -> None:
    """The timeout path has to say which nodes are in what state.

    containerlab prints only "Running postdeploy actions" while it retries the
    EOS CLI, so the container states are the entire diagnosis: `Exited (255)`
    on the routers with the customer hosts still up is what identifies a host
    that cannot boot EOS at all. A timeout that printed nothing would just be a
    faster dead end.
    """
    ctx = _RecordingContext()
    _report_stalled_deploy(ctx, "mpls-backbone-1", "ceos:test")  # type: ignore[arg-type]
    assert len(ctx.commands) == 1
    assert "docker ps -a" in ctx.commands[0]
    assert "name=clab-mpls-backbone-1-" in ctx.commands[0]


def test_the_deploy_timeout_is_a_positive_number_of_seconds() -> None:
    """A non-positive timeout would abort every deploy instantly."""
    assert LAB_DEPLOY_TIMEOUT_SECONDS > 0
