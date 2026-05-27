"""Unit tests for the BatfishBackboneCheck."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from checks.batfish_backbone import BatfishBackboneCheck
from checks.batfish_helpers import Finding


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


@pytest.mark.asyncio
async def test_nokia_pes_skipped_from_snapshot() -> None:
    """Nokia SR OS and SR Linux PEs are excluded; only the Arista PE appears in configs."""
    data = _backbone_data(
        [_pe("pe1", "arista_eos"), _pe("pe2", "nokia_sros"), _pe("pe3", "nokia_srlinux")]
    )
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock(return_value="! config\n")  # type: ignore[attr-defined]

    captured: list[str] = []

    def capture(**kwargs: Any) -> list[Any]:
        snapshot_dir = kwargs["snapshot_dir"]
        captured.extend(sorted(p.name for p in (snapshot_dir / "configs").iterdir()))
        return []

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=True),
        patch("checks.batfish_backbone.Session"),
        patch("checks.batfish_backbone.run_snapshot", side_effect=capture),
    ):
        await check.validate(data)

    assert captured == ["pe1.cfg"]
    assert check.errors == []


@pytest.mark.asyncio
async def test_missing_artifact_excluded_but_does_not_fail() -> None:
    """A PE whose artifact fetch returns None is silently excluded; others still run."""
    data = _backbone_data([_pe("pe1", "arista_eos"), _pe("pe2", "cisco_iosxr")])
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    # pe1 has no artifact yet, pe2 does.
    check._fetch_artifact = AsyncMock(side_effect=[None, "! pe2\n"])  # type: ignore[attr-defined]

    captured: list[str] = []

    def capture(**kwargs: Any) -> list[Any]:
        snapshot_dir = kwargs["snapshot_dir"]
        captured.extend(sorted(p.name for p in (snapshot_dir / "configs").iterdir()))
        return []

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=True),
        patch("checks.batfish_backbone.Session"),
        patch("checks.batfish_backbone.run_snapshot", side_effect=capture),
    ):
        await check.validate(data)

    assert captured == ["pe2.cfg"]
    assert check.errors == []


@pytest.mark.asyncio
async def test_all_pes_skipped_short_circuits() -> None:
    """When every PE is unsupported, validate() short-circuits before touching Batfish."""
    data = _backbone_data([_pe("pe1", "nokia_sros"), _pe("pe2", "nokia_srlinux")])
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock()  # type: ignore[attr-defined]

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=True),
        patch("checks.batfish_backbone.Session") as session_cls,
        patch("checks.batfish_backbone.run_snapshot") as run_snap,
    ):
        await check.validate(data)

    # Snapshot was never initialized.
    assert not session_cls.called
    assert not run_snap.called
    assert check.errors == []


@pytest.mark.asyncio
async def test_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """BATFISH_DISABLED=1 causes validate() to exit early without touching Batfish."""
    monkeypatch.setenv("BATFISH_DISABLED", "1")
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock()  # type: ignore[attr-defined]

    with patch("checks.batfish_backbone.Session") as session_cls:
        await check.validate(data)

    assert not session_cls.called
    assert check.errors == []


@pytest.mark.asyncio
async def test_batfish_unreachable_logs_error() -> None:
    """When wait_for_batfish returns False, an 'unreachable' error is recorded."""
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock(return_value="! pe1\n")  # type: ignore[attr-defined]

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=False),
        patch("checks.batfish_backbone.Session") as session_cls,
        patch("checks.batfish_backbone.run_snapshot") as run_snap,
    ):
        await check.validate(data)

    assert not session_cls.called
    assert not run_snap.called
    assert len(check.errors) == 1
    assert "unreachable" in check.errors[0]["message"]


@pytest.mark.asyncio
async def test_error_findings_become_check_errors() -> None:
    """Error-severity findings are added to check.errors; warnings are not."""
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock(return_value="! pe1\n")  # type: ignore[attr-defined]

    error_finding = Finding(
        severity="error",
        query="fileParseStatus",
        node="pe1",
        message="config configs/pe1.cfg parse status: FAILED",
        detail=None,
    )
    warning_finding = Finding(
        severity="warning",
        query="bgpSessionCompatibility",
        node="pe1",
        message="bgp half open",
        detail=None,
    )

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=True),
        patch("checks.batfish_backbone.Session"),
        patch(
            "checks.batfish_backbone.run_snapshot",
            return_value=[error_finding, warning_finding],
        ),
    ):
        await check.validate(data)

    assert len(check.errors) == 1
    assert "fileParseStatus" in check.errors[0]["message"]


@pytest.mark.asyncio
async def test_fetch_artifact_uses_name_filter_and_returns_body() -> None:
    """_fetch_artifact queries CoreArtifact by artifact name + PE id, downloads via object_store."""
    check = BatfishBackboneCheck(branch="main")

    artifact = MagicMock()
    artifact.storage_id = MagicMock(value="storage-abc")

    check.client = MagicMock()
    check.client.get = AsyncMock(return_value=artifact)
    check.client.object_store.get = AsyncMock(return_value="! pe1 config\n")

    body = await check._fetch_artifact(pe_id="id-pe1", platform_name="arista_eos")

    assert body == "! pe1 config\n"
    check.client.get.assert_awaited_once_with(
        kind="CoreArtifact",
        object__ids=["id-pe1"],
        name__value="pe-arista-eos",
    )
    check.client.object_store.get.assert_awaited_once_with(identifier="storage-abc")


@pytest.mark.asyncio
async def test_fetch_artifact_returns_none_when_not_found() -> None:
    """When the SDK raises NodeNotFoundError, _fetch_artifact returns None."""
    from infrahub_sdk.exceptions import NodeNotFoundError

    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check.client.get = AsyncMock(side_effect=NodeNotFoundError(identifier={"id": ["missing"]}))

    body = await check._fetch_artifact(pe_id="id-pe1", platform_name="arista_eos")
    assert body is None
