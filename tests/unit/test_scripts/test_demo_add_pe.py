"""Unit tests for `scripts/demo_add_pe.py`, the `invoke demo-add-device` workflow."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts import demo_add_pe

FINANCIAL_PES = [f"pe-0{n}" for n in range(1, 9)]


def _attr(value: str) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def _pe(name: str, site: str = "fra", device_type: str = "7280R3") -> SimpleNamespace:
    """A PE device with the fields `resolve_request` reads."""
    return SimpleNamespace(
        name=_attr(name),
        location=SimpleNamespace(peer=SimpleNamespace(shortname=_attr(site))),
        device_type=SimpleNamespace(peer=SimpleNamespace(name=_attr(device_type))),
    )


def _resolve(**overrides: Any) -> tuple[demo_add_pe.PeRequest | None, list[str]]:
    """Resolve against an eight-PE financial-style backbone, with options overridden."""
    kwargs: dict[str, Any] = {
        "pes": [_pe(name, site="lon" if name == "pe-01" else "fra") for name in FINANCIAL_PES],
        "backbones": {"mpls-backbone-1": FINANCIAL_PES},
        "existing_device_names": set(FINANCIAL_PES) | {"ce-ib-lon"},
        "name": None,
        "uplinks": None,
        "platform": "arista_eos",
        "site": None,
        "device_type": None,
    }
    kwargs.update(overrides)
    return demo_add_pe.resolve_request(**kwargs)


def test_next_pe_name_follows_the_highest_numbered_pe() -> None:
    assert demo_add_pe.next_pe_name(set(FINANCIAL_PES) | {"ce-ib-lon"}) == "pe-09"


def test_next_pe_name_starts_at_one_when_no_name_is_numbered() -> None:
    """The isp dataset names PEs by city, so no `pe-NN` is in use."""
    assert demo_add_pe.next_pe_name({"pe-lon-arista", "pe-par-nokia"}) == "pe-01"


def test_defaults_give_the_next_pe_on_the_first_two_uplinks() -> None:
    request, errors = _resolve()
    assert errors == []
    assert request == demo_add_pe.PeRequest(
        name="pe-09",
        backbone="mpls-backbone-1",
        backbone_pes=FINANCIAL_PES,
        uplinks=["pe-01", "pe-02"],
        platform="arista_eos",
        site="lon",
        device_type="7280R3",
    )


def test_explicit_options_win_over_the_defaults() -> None:
    request, errors = _resolve(
        name="pe-20", uplinks=["pe-02", "pe-05"], site="ams", device_type="7800R3"
    )
    assert errors == []
    assert request is not None
    assert (request.name, request.uplinks, request.site, request.device_type) == (
        "pe-20",
        ["pe-02", "pe-05"],
        "ams",
        "7800R3",
    )


def test_site_and_device_type_come_from_the_first_uplink() -> None:
    request, _ = _resolve(uplinks=["pe-02", "pe-01"])
    assert request is not None
    assert request.site == "fra"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"name": "pe-01"}, "already exists"),
        ({"uplinks": ["pe-02"]}, "at least 2"),
        ({"uplinks": ["pe-02", "pe-99"]}, "Not in the selected backbone"),
        ({"platform": "cisco_viptela"}, "Unsupported PE platform"),
        ({"uplinks": ["pe-01", "pe-02", "pe-03", "pe-04", "pe-05"]}, "at most 4"),
    ],
)
def test_invalid_requests_are_refused_before_a_branch_exists(
    overrides: dict[str, Any], expected: str
) -> None:
    """The page's form rules apply, plus its four-uplink limit."""
    request, errors = _resolve(**overrides)
    assert request is None
    assert any(expected in error for error in errors), errors


def test_no_backbone_is_reported() -> None:
    request, errors = _resolve(backbones={})
    assert request is None
    assert "invoke bootstrap" in errors[0]


REQUEST = demo_add_pe.PeRequest(
    name="pe-09",
    backbone="mpls-backbone-1",
    backbone_pes=FINANCIAL_PES,
    uplinks=["pe-02", "pe-05"],
    platform="arista_eos",
    site="fra",
    device_type="7280R3",
)


def _clients() -> tuple[MagicMock, MagicMock]:
    """A main-branch client and a feature-branch client, both recording calls."""
    main_client = MagicMock()
    branch_client = MagicMock()
    branch_client.create.return_value = MagicMock(id="request-id")
    branch_client.all.return_value = [SimpleNamespace(id="def-1"), SimpleNamespace(id="def-2")]
    main_client.create.return_value = MagicMock(id="pc-id")
    return main_client, branch_client


def _finished(count_sessions: int = 8) -> dict[str, Any]:
    return {
        "TopologyPeOnboarding": {
            "edges": [{"node": {"status": {"value": "active"}, "device": {"node": {"id": "d"}}}}]
        },
        "InterfacePhysical": {"count": 2},
        "RoutingBGPSession": {"count": count_sessions},
    }


def test_run_follows_the_add_pe_page_and_prints_the_link(capsys: pytest.CaptureFixture) -> None:
    """Branch, request, group, generator, artifacts on the branch, then the proposed change."""
    main_client, branch_client = _clients()
    branch_client.execute_graphql.return_value = _finished()

    with (
        patch.object(demo_add_pe, "InfrahubClientSync", side_effect=[main_client, branch_client]),
        patch.object(demo_add_pe, "_trigger_generate") as trigger,
        patch.object(demo_add_pe.time, "sleep"),
    ):
        rc = demo_add_pe.run(REQUEST, "backbone/add-pe-pe-09-x", 30, "http://infrahub:8000")

    assert rc == 0
    main_client.branch.create.assert_called_once_with(
        "backbone/add-pe-pe-09-x", sync_with_git=False
    )
    request_kwargs = branch_client.create.call_args.kwargs
    assert request_kwargs["kind"] == "TopologyPeOnboarding"
    assert request_kwargs["uplinks"] == [{"hfid": ["pe-02"]}, {"hfid": ["pe-05"]}]
    branch_client.get.return_value.add_relationships.assert_called_once_with(
        relation_to_update="members", related_nodes=["request-id"]
    )
    assert [call.args for call in trigger.call_args_list] == [
        (branch_client, "def-1", "backbone/add-pe-pe-09-x"),
        (branch_client, "def-2", "backbone/add-pe-pe-09-x"),
    ]
    pc_kwargs = main_client.create.call_args.kwargs
    assert pc_kwargs == {
        "kind": "CoreProposedChange",
        "source_branch": "backbone/add-pe-pe-09-x",
        "destination_branch": "main",
        "name": "Add PE pe-09",
    }
    out = capsys.readouterr().out
    assert "http://infrahub:8000/proposed-changes/pc-id" in out
    assert "lab.deploy --branch backbone/add-pe-pe-09-x" in out


def test_run_stops_without_a_proposed_change_when_the_generator_times_out() -> None:
    """An incomplete PE must not get artifacts or a proposed change, as on the page."""
    main_client, branch_client = _clients()
    # One session short of the full mesh: the generator is still writing.
    branch_client.execute_graphql.return_value = _finished(count_sessions=7)

    with (
        patch.object(demo_add_pe, "InfrahubClientSync", side_effect=[main_client, branch_client]),
        patch.object(demo_add_pe, "_trigger_generate") as trigger,
        patch.object(demo_add_pe.time, "sleep"),
        patch.object(demo_add_pe.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 999.0]),
    ):
        rc = demo_add_pe.run(REQUEST, "b", 30, "http://infrahub:8000")

    assert rc == 1
    trigger.assert_not_called()
    main_client.create.assert_not_called()


def test_one_complete_reading_is_not_enough() -> None:
    """Two consecutive complete readings end the wait, so one mid-write reading cannot."""
    _, branch_client = _clients()
    branch_client.execute_graphql.side_effect = [
        _finished(),
        _finished(7),
        _finished(),
        _finished(),
    ]
    with patch.object(demo_add_pe.time, "sleep"):
        assert demo_add_pe._wait_for_generator(branch_client, REQUEST, "b", 30)
    assert branch_client.execute_graphql.call_count == 4


def test_main_refuses_invalid_options_without_creating_anything() -> None:
    main_client = MagicMock()
    with (
        patch.object(demo_add_pe, "InfrahubClientSync", return_value=main_client),
        patch.object(
            demo_add_pe,
            "_read_main",
            return_value=(
                [_pe(n) for n in FINANCIAL_PES],
                {"mpls-backbone-1": FINANCIAL_PES},
                set(FINANCIAL_PES),
            ),
        ),
        patch.object(demo_add_pe, "run") as run,
    ):
        assert demo_add_pe.main(["--name", "pe-01"]) == 1
    run.assert_not_called()


def test_main_passes_the_named_branch_and_ui_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFRAHUB_UI_URL", "http://fal.example:8000/")
    with (
        patch.object(demo_add_pe, "InfrahubClientSync"),
        patch.object(
            demo_add_pe,
            "_read_main",
            return_value=(
                [_pe(n) for n in FINANCIAL_PES],
                {"mpls-backbone-1": FINANCIAL_PES},
                set(FINANCIAL_PES),
            ),
        ),
        patch.object(demo_add_pe, "run", return_value=0) as run,
    ):
        assert demo_add_pe.main(["--uplinks", "pe-02, pe-05", "--branch", "demo"]) == 0
    request, branch, _wait, ui_url = run.call_args.args
    assert request.uplinks == ["pe-02", "pe-05"]
    assert branch == "demo"
    assert ui_url == "http://fal.example:8000"
