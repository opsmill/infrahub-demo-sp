"""Unit tests for the BatfishBackboneCheck.

The check no longer drives pybatfish directly; it POSTs configs to the
batfish-runner sidecar over HTTP. These tests mock the ``httpx.AsyncClient``
seam so the worker-side logic (PE partitioning, artifact assembly, payload
shape, finding emission, error handling) is exercised without a live runner.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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


def _response(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    """Build a mock ``httpx.Response`` with sync ``.json()`` and a status code."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_body if json_body is not None else {"findings": []})
    resp.text = ""
    return resp


def _patch_httpx(
    *, response: MagicMock | None = None, raise_exc: Exception | None = None
) -> tuple[MagicMock, AsyncMock]:
    """Patch ``httpx.AsyncClient`` to a fake async-context client.

    Returns the ``AsyncClient`` factory mock and the ``post`` AsyncMock so tests
    can assert call counts and inspect the posted payload.
    """
    post = AsyncMock(side_effect=raise_exc) if raise_exc else AsyncMock(return_value=response)
    client = MagicMock()
    client.post = post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory, post


def _check_with_artifact(body: str | list | None = "! config\n") -> BatfishBackboneCheck:
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    if isinstance(body, list):
        check._fetch_artifact = AsyncMock(side_effect=body)  # type: ignore[attr-defined]
    else:
        check._fetch_artifact = AsyncMock(return_value=body)  # type: ignore[attr-defined]
    return check


@pytest.mark.asyncio
async def test_happy_path_posts_all_configs_no_findings() -> None:
    """Three supported PEs → one POST carrying all three configs; no errors."""
    data = _backbone_data(
        [_pe("pe1", "arista_eos"), _pe("pe2", "cisco_iosxr"), _pe("pe3", "juniper_junos")]
    )
    check = _check_with_artifact()
    factory, post = _patch_httpx(response=_response(json_body={"findings": []}))

    with patch("checks.batfish_backbone.httpx.AsyncClient", factory):
        await check.validate(data)

    assert check.errors == []
    post.assert_awaited_once()
    payload = post.await_args.kwargs["json"]
    assert sorted(payload["configs"]) == ["pe1", "pe2", "pe3"]
    assert payload["expected_hosts"] == ["pe1", "pe2", "pe3"]
    assert payload["network"] == "infrahub-mpls"


@pytest.mark.asyncio
async def test_nokia_pes_excluded_from_payload() -> None:
    """Nokia SR OS and SR Linux PEs are filtered before the POST."""
    data = _backbone_data(
        [_pe("pe1", "arista_eos"), _pe("pe2", "nokia_sros"), _pe("pe3", "nokia_srlinux")]
    )
    check = _check_with_artifact()
    factory, post = _patch_httpx(response=_response())

    with patch("checks.batfish_backbone.httpx.AsyncClient", factory):
        await check.validate(data)

    assert list(post.await_args.kwargs["json"]["configs"]) == ["pe1"]
    assert check.errors == []


@pytest.mark.asyncio
async def test_missing_artifact_excluded_but_does_not_fail() -> None:
    """A PE whose artifact fetch returns None is excluded; others still post."""
    data = _backbone_data([_pe("pe1", "arista_eos"), _pe("pe2", "cisco_iosxr")])
    check = _check_with_artifact(body=[None, "! pe2\n"])  # pe1 missing, pe2 present
    factory, post = _patch_httpx(response=_response())

    with patch("checks.batfish_backbone.httpx.AsyncClient", factory):
        await check.validate(data)

    assert list(post.await_args.kwargs["json"]["configs"]) == ["pe2"]
    assert check.errors == []


@pytest.mark.asyncio
async def test_all_pes_skipped_short_circuits() -> None:
    """When every PE is unsupported, validate() never contacts the runner."""
    data = _backbone_data([_pe("pe1", "nokia_sros"), _pe("pe2", "nokia_srlinux")])
    check = _check_with_artifact(body=None)
    factory, _ = _patch_httpx(response=_response())

    with patch("checks.batfish_backbone.httpx.AsyncClient", factory):
        await check.validate(data)

    assert not factory.called
    assert check.errors == []


@pytest.mark.asyncio
async def test_no_artifacts_short_circuits() -> None:
    """Supported PEs but no rendered artifacts → no POST, no error."""
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = _check_with_artifact(body=None)
    factory, _ = _patch_httpx(response=_response())

    with patch("checks.batfish_backbone.httpx.AsyncClient", factory):
        await check.validate(data)

    assert not factory.called
    assert check.errors == []


@pytest.mark.asyncio
async def test_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """BATFISH_DISABLED=1 exits early without contacting the runner."""
    monkeypatch.setenv("BATFISH_DISABLED", "1")
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = _check_with_artifact()
    factory, _ = _patch_httpx(response=_response())

    with patch("checks.batfish_backbone.httpx.AsyncClient", factory):
        await check.validate(data)

    assert not factory.called
    assert check.errors == []


@pytest.mark.asyncio
async def test_runner_unreachable_logs_error() -> None:
    """A transport error POSTing to the runner fails the check loudly."""
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = _check_with_artifact()
    factory, _ = _patch_httpx(raise_exc=httpx.ConnectError("connection refused"))

    with patch("checks.batfish_backbone.httpx.AsyncClient", factory):
        await check.validate(data)

    assert len(check.errors) == 1
    assert "unreachable" in check.errors[0]["message"]


@pytest.mark.asyncio
async def test_runner_non_200_logs_error() -> None:
    """A non-200 response (e.g. Batfish unreachable upstream) fails the check."""
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = _check_with_artifact()
    factory, _ = _patch_httpx(
        response=_response(status_code=503, json_body={"error": "Batfish unreachable"})
    )

    with patch("checks.batfish_backbone.httpx.AsyncClient", factory):
        await check.validate(data)

    assert len(check.errors) == 1
    assert "503" in check.errors[0]["message"]
    assert "Batfish unreachable" in check.errors[0]["message"]


@pytest.mark.asyncio
async def test_error_findings_become_check_errors() -> None:
    """Error-severity findings from the runner are added to check.errors; warnings are not."""
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = _check_with_artifact()
    findings = {
        "findings": [
            {
                "severity": "error",
                "query": "fileParseStatus",
                "node": "pe1",
                "message": "config configs/pe1.cfg parse status: PARSE_FAIL",
                "detail": None,
            },
            {
                "severity": "warning",
                "query": "bgpSessionCompatibility",
                "node": "pe1",
                "message": "bgp half open",
                "detail": None,
            },
        ]
    }
    factory, _ = _patch_httpx(response=_response(json_body=findings))

    with patch("checks.batfish_backbone.httpx.AsyncClient", factory):
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
