"""Host-architecture to cEOS-image mapping used by ``invoke lab.deploy``.

The mirror publishes no multi-arch cEOS tag, so ``lab.deploy`` has to choose the
image from the host CPU. Getting this wrong is expensive to debug: the wrong
architecture does not fail at deploy, it fails minutes later as an unexplained
stall in containerlab's postdeploy step.
"""

from __future__ import annotations

import pytest

from tasks import CEOS_IMAGE_AMD64, CEOS_IMAGE_ARM64, _ceos_image_for_machine


@pytest.mark.parametrize("machine", ["aarch64", "arm64", "ARM64", "armv8l"])
def test_arm64_hosts_get_the_arm64_image(machine: str) -> None:
    """Every 64-bit ARM spelling maps to the arm64 image.

    ``aarch64`` is what Apple Silicon reports through a Linux VM (OrbStack,
    Lima, UTM), which is the case that sent a customer chasing a phantom
    resource problem, so the spellings are covered explicitly.
    """
    assert _ceos_image_for_machine(machine) == CEOS_IMAGE_ARM64


@pytest.mark.parametrize("machine", ["x86_64", "amd64", "AMD64"])
def test_x86_hosts_get_the_amd64_image(machine: str) -> None:
    """Intel/AMD hosts map to the amd64 image."""
    assert _ceos_image_for_machine(machine) == CEOS_IMAGE_AMD64


def test_unknown_architecture_falls_back_to_amd64() -> None:
    """An unrecognised architecture takes amd64 rather than guessing.

    amd64 is the architecture the demo is regularly exercised on, and an
    unpullable image fails immediately and legibly at ``docker pull`` — better
    than a silent mismatch that surfaces as a postdeploy hang.
    """
    assert _ceos_image_for_machine("riscv64") == CEOS_IMAGE_AMD64


@pytest.mark.parametrize("image", [CEOS_IMAGE_AMD64, CEOS_IMAGE_ARM64])
def test_both_images_are_cgroup_v2_capable(image: str) -> None:
    """Neither image predates 4.32.0F.

    cEOS-lab builds older than 4.32.0F require a cgroups v1 host and never
    finish booting on a cgroups v2 one — the default on Ubuntu 21.04+, OrbStack
    and most current distros. The failure is silent, so it is worth a test
    rather than a comment.
    """
    major, minor = (int(part) for part in image.rsplit(":", 1)[1].split(".")[:2])
    assert (major, minor) >= (4, 32)
