"""Exercise Add PE submission with mocked Infrahub and Streamlit boundaries."""

from __future__ import annotations

import asyncio
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from service_catalog.utils import validators

PAGE = Path("service_catalog/pages/4_Add_PE.py")


class StopPage(Exception):
    """Stand in for Streamlit stopping execution of the page."""


@pytest.fixture
def page_boundaries(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    """Supply valid form input and stub every external call the page can make."""

    def attribute(value: str | None) -> SimpleNamespace:
        return SimpleNamespace(value=value)

    peers = [SimpleNamespace(id=name, name=attribute(name)) for name in ("pe-02", "pe-07")]
    backbone = SimpleNamespace(
        name=attribute("mpls-backbone-1"),
        pes=SimpleNamespace(fetch=AsyncMock(), peers=peers),
    )
    platforms = [
        SimpleNamespace(name=attribute(name), containerlab_os=attribute(None))
        for name in (
            "arista_eos",
            "cisco_iosxr",
            "juniper_junos",
            "nokia_sros",
            "cisco_viptela",
            "versa_flexvnf",
        )
    ]
    rows = {
        "DcimDevice": peers,
        "TopologyMplsBackbone": [backbone],
        "LocationSite": [SimpleNamespace(name=attribute("Milan"), shortname=attribute("mil"))],
        "DcimPlatform": platforms,
        "DcimDeviceType": [SimpleNamespace(name=attribute("7280R3"))],
        "CoreArtifactDefinition": [],
    }
    client = MagicMock()
    client.filters = AsyncMock(return_value=peers)
    client.all = AsyncMock(side_effect=lambda kind: rows[kind])
    client.branch.create = AsyncMock()
    client.get = AsyncMock(return_value=SimpleNamespace(add_relationships=AsyncMock()))
    client.create = AsyncMock(return_value=SimpleNamespace(id="new-id", save=AsyncMock()))
    ui = MagicMock()
    ui.selectbox.side_effect = lambda label, options: options[0]
    ui.text_input.side_effect = ["pe-09", ""]
    ui.multiselect.return_value = ["pe-02", "pe-07"]
    ui.form_submit_button.return_value = True
    ui.stop.side_effect = StopPage
    module = ModuleType("utils")
    monkeypatch.setattr(module, "client_for", lambda **kwargs: client, raising=False)
    monkeypatch.setattr(module, "run_async", asyncio.run, raising=False)
    monkeypatch.setitem(sys.modules, "utils", module)
    monkeypatch.setitem(sys.modules, "utils.validators", validators)
    monkeypatch.setitem(sys.modules, "streamlit", ui)
    monkeypatch.setenv("GENERATOR_WAIT_TIME", "0")
    return ui, client


def test_timeout_stops_before_artifacts_or_proposed_change(page_boundaries: tuple) -> None:
    ui, client = page_boundaries
    with pytest.raises(StopPage):
        runpy.run_path(str(PAGE), run_name="__main__")
    assert [call.kwargs["kind"] for call in client.create.await_args_list] == [
        "TopologyPeOnboarding"
    ]
    assert not any(
        call.kwargs["kind"] == "CoreArtifactDefinition" for call in client.all.await_args_list
    )
    ui.success.assert_not_called()


def test_platform_options_exclude_sdwan_platforms(page_boundaries: tuple) -> None:
    ui, _ = page_boundaries
    try:
        runpy.run_path(str(PAGE), run_name="__main__")
    except StopPage:
        pass
    platform_call = next(call for call in ui.selectbox.call_args_list if call.args[0] == "Platform")
    assert {label.split()[0] for label in platform_call.kwargs["options"]} == {
        "arista_eos",
        "cisco_iosxr",
        "juniper_junos",
        "nokia_sros",
    }


def test_completed_generation_opens_proposed_change(
    page_boundaries: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, client = page_boundaries
    client.execute_graphql = AsyncMock(
        return_value={
            "TopologyPeOnboarding": {
                "edges": [
                    {"node": {"status": {"value": "active"}, "device": {"node": {"id": "pe-09"}}}}
                ]
            },
            "InterfacePhysical": {"count": 2},
            "RoutingBGPSession": {"count": 2},
        }
    )
    monkeypatch.setenv("GENERATOR_WAIT_TIME", "180")
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    runpy.run_path(str(PAGE), run_name="__main__")
    assert [call.kwargs["kind"] for call in client.create.await_args_list] == [
        "TopologyPeOnboarding",
        "CoreProposedChange",
    ]
    assert client.execute_graphql.await_count == 2
    ui.stop.assert_not_called()
    ui.success.assert_called_once()


def _completed_generation(client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the generator poll succeed and give the page one artifact definition."""
    client.execute_graphql = AsyncMock(
        return_value={
            "TopologyPeOnboarding": {
                "edges": [
                    {"node": {"status": {"value": "active"}, "device": {"node": {"id": "pe-09"}}}}
                ]
            },
            "InterfacePhysical": {"count": 2},
            "RoutingBGPSession": {"count": 2},
        }
    )
    client.address = "http://infrahub:8000"
    definition = SimpleNamespace(id="def-1", name=SimpleNamespace(value="pe-arista-eos-config"))
    rows = {"CoreArtifactDefinition": [definition]}
    previous = client.all.side_effect
    client.all.side_effect = lambda kind: rows.get(kind) or previous(kind)
    monkeypatch.setenv("GENERATOR_WAIT_TIME", "180")
    monkeypatch.setattr("time.sleep", lambda seconds: None)


def test_missing_token_stops_before_artifacts_or_proposed_change(
    page_boundaries: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, client = page_boundaries
    _completed_generation(client, monkeypatch)
    monkeypatch.delenv("INFRAHUB_API_TOKEN", raising=False)
    urlopen = MagicMock()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(StopPage):
        runpy.run_path(str(PAGE), run_name="__main__")
    urlopen.assert_not_called()
    assert "INFRAHUB_API_TOKEN is not set" in ui.error.call_args.args[0]
    assert [call.kwargs["kind"] for call in client.create.await_args_list] == [
        "TopologyPeOnboarding"
    ]


def test_artifact_request_failure_stops_before_proposed_change(
    page_boundaries: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, client = page_boundaries
    _completed_generation(client, monkeypatch)
    monkeypatch.setenv("INFRAHUB_API_TOKEN", "token")
    urlopen = MagicMock(side_effect=TimeoutError("timed out"))
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(StopPage):
        runpy.run_path(str(PAGE), run_name="__main__")
    assert urlopen.call_args.kwargs["timeout"] == 60
    assert "pe-arista-eos-config failed: timed out" in ui.error.call_args.args[0]
    assert [call.kwargs["kind"] for call in client.create.await_args_list] == [
        "TopologyPeOnboarding"
    ]
