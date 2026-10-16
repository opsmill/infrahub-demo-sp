"""Unit tests for the pe_onboarding check."""

from __future__ import annotations

import pytest

from checks.pe_onboarding import PeOnboardingCheck


def _port(status: str = "free", description: str = "Customer-facing (free)") -> dict:
    return {
        "node": {
            "__typename": "InterfacePhysical",
            "status": {"value": status},
            "description": {"value": description},
        }
    }


def _uplink(name: str, role: str = "pe", ports: list[dict] | None = None) -> dict:
    return {
        "node": {
            "name": {"value": name},
            "role": {"value": role},
            "interfaces": {"edges": ports if ports is not None else [_port()]},
        }
    }


def _data(name: str = "pe-09", uplinks: list[dict] | None = None, backbone_pes: tuple = ()) -> dict:
    return {
        "TopologyPeOnboarding": {
            "edges": [
                {
                    "node": {
                        "name": {"value": name},
                        "status": {"value": "active"},
                        "device": {"node": {"id": f"dev-{name}", "name": {"value": name}}},
                        "backbone": {
                            "node": {
                                "name": {"value": "mpls-backbone-1"},
                                "pes": {
                                    "edges": [
                                        {"node": {"name": {"value": pe}}}
                                        for pe in (*backbone_pes, name)
                                    ]
                                },
                            }
                        },
                        "uplinks": {"edges": uplinks or []},
                    }
                }
            ]
        },
        # The device the generator created for this request, and no other.
        "DcimDevice": {"edges": [{"node": {"id": f"dev-{name}"}}]},
        "InterfacePhysical": {"count": len(uplinks or [])},
        "RoutingBGPSession": {"count": len(backbone_pes)},
    }


async def _errors(data: dict) -> list[str]:
    check = PeOnboardingCheck(branch="main")
    await check.validate(data)
    return [error["message"] for error in check.errors]


@pytest.mark.asyncio
async def test_two_backbone_pes_with_free_ports_pass() -> None:
    data = _data(uplinks=[_uplink("pe-02"), _uplink("pe-07")], backbone_pes=("pe-02", "pe-07"))
    assert await _errors(data) == []


@pytest.mark.asyncio
async def test_one_uplink_fails() -> None:
    data = _data(uplinks=[_uplink("pe-02")], backbone_pes=("pe-02",))
    errors = await _errors(data)
    assert len(errors) == 1
    assert "at least 2 distinct uplinks" in errors[0]


@pytest.mark.asyncio
async def test_duplicate_uplink_counts_once() -> None:
    data = _data(uplinks=[_uplink("pe-02"), _uplink("pe-02")], backbone_pes=("pe-02",))
    assert any("at least 2 distinct uplinks" in e for e in await _errors(data))


@pytest.mark.asyncio
async def test_self_uplink_fails() -> None:
    data = _data(uplinks=[_uplink("pe-09"), _uplink("pe-02")], backbone_pes=("pe-02",))
    errors = await _errors(data)
    assert any("cannot uplink to itself" in e for e in errors)
    # The self-uplink is not also reported as "not a PE" or "not in backbone".
    assert not any("pe-09 is not" in e for e in errors)


@pytest.mark.asyncio
async def test_non_pe_uplink_fails() -> None:
    data = _data(
        uplinks=[_uplink("ce-trading-lon", role="cpe"), _uplink("pe-02")], backbone_pes=("pe-02",)
    )
    errors = await _errors(data)
    assert any("ce-trading-lon is not a PE" in e for e in errors)
    assert any("ce-trading-lon is not in backbone mpls-backbone-1" in e for e in errors)


@pytest.mark.asyncio
async def test_uplink_outside_backbone_fails() -> None:
    data = _data(uplinks=[_uplink("pe-02"), _uplink("pe-99")], backbone_pes=("pe-02",))
    assert ["pe-09: uplink pe-99 is not in backbone mpls-backbone-1"] == await _errors(data)


@pytest.mark.asyncio
async def test_uplink_without_free_port_fails() -> None:
    full = _uplink("pe-07", ports=[_port(status="active", description="To pe-03")])
    data = _data(uplinks=[_uplink("pe-02"), full], backbone_pes=("pe-02", "pe-07"))
    assert ["pe-09: uplink pe-07 has no free port"] == await _errors(data)


@pytest.mark.asyncio
async def test_port_already_claimed_for_this_pe_passes() -> None:
    """After the generator ran, the claimed port is active with our description: still valid."""
    claimed = _uplink("pe-07", ports=[_port(status="active", description="To pe-09")])
    data = _data(uplinks=[_uplink("pe-02"), claimed], backbone_pes=("pe-02", "pe-07"))
    assert await _errors(data) == []


@pytest.mark.asyncio
async def test_empty_data_produces_no_errors() -> None:
    assert await _errors({"TopologyPeOnboarding": {"edges": []}}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["active_status", "device", "core_ports", "sessions"])
async def test_incomplete_generation_fails(missing: str) -> None:
    data = _data(uplinks=[_uplink("pe-02"), _uplink("pe-07")], backbone_pes=("pe-02", "pe-07"))
    request = data["TopologyPeOnboarding"]["edges"][0]["node"]
    if missing == "active_status":
        request["status"]["value"] = "draft"
    elif missing == "device":
        request["device"]["node"] = None
    elif missing == "core_ports":
        data["InterfacePhysical"]["count"] = 1
    else:
        data["RoutingBGPSession"]["count"] = 1
    assert any("not fully materialised" in error for error in await _errors(data))


@pytest.mark.asyncio
async def test_later_core_links_do_not_invalidate_an_existing_request() -> None:
    data = _data(uplinks=[_uplink("pe-02"), _uplink("pe-07")], backbone_pes=("pe-02", "pe-07"))
    data["InterfacePhysical"]["count"] = 3
    assert await _errors(data) == []


@pytest.mark.asyncio
async def test_decommissioned_request_is_skipped() -> None:
    """A retired request must not fail later proposed changes, even though it is incomplete."""
    data = _data(uplinks=[_uplink("pe-02")], backbone_pes=())
    request = data["TopologyPeOnboarding"]["edges"][0]["node"]
    request["status"]["value"] = "decommissioned"
    request["device"]["node"] = None
    assert await _errors(data) == []


@pytest.mark.asyncio
async def test_a_name_taken_by_another_device_fails() -> None:
    """A request loaded from YAML can name a device that already exists.

    The generator refuses to adopt it, so the request never links a device,
    and the check says why rather than only that generation is incomplete.
    """
    data = _data(uplinks=[_uplink("pe-02"), _uplink("pe-07")], backbone_pes=("pe-02", "pe-07"))
    request = data["TopologyPeOnboarding"]["edges"][0]["node"]
    request["device"] = None
    request["status"] = {"value": "draft"}
    data["DcimDevice"] = {"edges": [{"node": {"id": "someone-elses"}}]}
    errors = await _errors(data)
    assert any("already exists and was not created by this request" in e for e in errors)


@pytest.mark.asyncio
async def test_the_device_the_request_created_does_not_count_as_taken() -> None:
    data = _data(uplinks=[_uplink("pe-02"), _uplink("pe-07")], backbone_pes=("pe-02", "pe-07"))
    assert not [e for e in await _errors(data) if "already exists" in e]
