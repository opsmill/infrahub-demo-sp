"""Catalog test: service generator registration in .infrahub.yml.

Guards the two-part fix for the service-catalog wizards (SD-WAN and
L3VPN) not producing config artifacts:

1. Each wizard runs its generator explicitly before rendering artifacts
   (covered in ``pages/2_Create_SDWAN.py`` / ``pages/1_Create_L3VPN.py``).
2. Neither generator may re-run inside the proposed-change pipeline
   (``execute_in_proposed_change: false``). Their queries return the
   objects they create (sdwan_edge / lan_address; vrf / pe_interface /
   pe_address / ce_address); re-running as a pipeline check destabilises
   the internal query-group update and deletes the freshly-rendered data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _generators() -> dict[str, dict]:
    cfg = yaml.safe_load(Path(".infrahub.yml").read_text())
    return {g["name"]: g for g in cfg.get("generator_definitions", [])}


def test_sdwan_generator_registered() -> None:
    g = _generators()["generate_sdwan"]
    assert g["class_name"] == "SdwanGenerator"
    assert g["file_path"] == "generators/generate_sdwan.py"
    assert g["targets"] == "sdwans"
    assert Path(g["file_path"]).exists()


@pytest.mark.parametrize("generator_name", ["generate_sdwan", "generate_l3vpn"])
def test_generator_not_run_in_proposed_change(generator_name: str) -> None:
    assert _generators()[generator_name]["execute_in_proposed_change"] is False
