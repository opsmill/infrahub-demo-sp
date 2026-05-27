"""Unit tests for the BatfishBackboneCheck."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from checks.batfish_backbone import BatfishBackboneCheck


def _pe(name: str, platform: str) -> dict:
    return {
        "node": {
            "id": f"id-{name}",
            "name": {"value": name},
            "platform": {"node": {"name": {"value": platform}, "containerlab_os": {"value": ""}}},
        }
    }


def _backbone_data(pes: list[dict]) -> dict:
    return {
        "TopologyMplsBackbone": {
            "edges": [{"node": {"name": {"value": "mpls-backbone"}, "pes": {"edges": pes}}}]
        }
    }


@pytest.mark.asyncio
async def test_happy_path_no_findings() -> None:
    """Three supported PEs, all configs parse cleanly, no findings → no errors."""
    data = _backbone_data(
        [_pe("pe1", "arista_eos"), _pe("pe2", "cisco_iosxr"), _pe("pe3", "juniper_junos")]
    )
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock(return_value="! config\n")  # type: ignore[attr-defined]

    # Capture the configs directory contents during the run_snapshot call —
    # the tempdir is cleaned up when validate() returns, so post-call inspection
    # would race with TemporaryDirectory.__exit__.
    captured_configs: list[str] = []

    def capture(**kwargs: Any) -> list[Any]:
        snapshot_dir = kwargs["snapshot_dir"]
        captured_configs.extend(sorted(p.name for p in (snapshot_dir / "configs").iterdir()))
        return []

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=True),
        patch("checks.batfish_backbone.Session") as session_cls,
        patch("checks.batfish_backbone.run_snapshot", side_effect=capture) as run_snap,
    ):
        await check.validate(data)

    assert check.errors == []
    assert session_cls.called
    assert run_snap.called
    assert captured_configs == ["pe1.cfg", "pe2.cfg", "pe3.cfg"]
