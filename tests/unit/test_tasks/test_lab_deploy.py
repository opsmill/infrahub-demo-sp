"""Invoke tasks that drive Infrahub workflows: `lab.deploy --branch` and `demo-add-device`."""

from __future__ import annotations

from pathlib import Path

import pytest

import tasks


class _RecordingContext:
    """Minimal Invoke-context stand-in that records the commands it is given."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, **_kwargs: object) -> None:
        """Record a command instead of running it."""
        self.commands.append(command)


@pytest.fixture
def lab_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the lab paths at a temporary directory holding a minimal topology.

    The deploy reads the lab name from the fetched topology, and the recording
    context fetches nothing, so the file has to exist beforehand.
    """
    lab_dir = tmp_path / "lab"
    lab_dir.mkdir()
    (lab_dir / "mpls-backbone.clab.yml").write_text("name: mpls-backbone-1\n")
    monkeypatch.setattr(tasks, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(tasks, "LAB_DIR", lab_dir)
    monkeypatch.setattr(tasks, "LAB_TOPO", lab_dir / "mpls-backbone.clab.yml")
    monkeypatch.setattr(tasks, "LAB_DEVICES_DIR", lab_dir / "devices")
    monkeypatch.setattr(tasks, "_assert_ceos_runnable", lambda *_a, **_kw: None)
    return lab_dir


def _infrahub_steps(commands: list[str]) -> list[str]:
    """Return the commands that read from or render in Infrahub."""
    scripts = ("regenerate_artifacts.py", "fetch_artifact.py", "fetch_lab_configs.py")
    return [cmd for cmd in commands if any(script in cmd for script in scripts)]


def test_branch_reaches_every_infrahub_step(lab_paths: Path) -> None:
    """Regenerate, topology fetch and config fetch all name the branch.

    If any one of them fell back to main, the lab would boot main's topology
    with the branch's configs, or the reverse, and a PE that exists only in the
    proposed change would be missing or have no startup-config.
    """
    ctx = _RecordingContext()
    tasks.lab_deploy.body(ctx, branch="backbone/add-pe-pe-09-1234")  # type: ignore[arg-type]
    steps = _infrahub_steps(ctx.commands)
    assert len(steps) == 3
    for step in steps:
        assert "--branch backbone/add-pe-pe-09-1234" in step


def test_no_branch_leaves_every_infrahub_step_on_the_default(lab_paths: Path) -> None:
    """Without ``--branch`` the scripts get no branch flag, so main stays the default."""
    ctx = _RecordingContext()
    tasks.lab_deploy.body(ctx)  # type: ignore[arg-type]
    steps = _infrahub_steps(ctx.commands)
    assert len(steps) == 3
    assert not [step for step in steps if "--branch" in step]


def test_demo_add_device_passes_only_the_options_given() -> None:
    """Empty options are left out so the script's own defaults apply."""
    ctx = _RecordingContext()
    tasks.demo_add_device.body(ctx, uplinks="pe-02,pe-05", device_type="7280R3")  # type: ignore[arg-type]
    assert ctx.commands == [
        "uv run python scripts/demo_add_pe.py --uplinks pe-02,pe-05 --device-type 7280R3"
    ]
