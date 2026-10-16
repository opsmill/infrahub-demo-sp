"""Unit tests for the PE onboarding helpers in generators/common.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import (
    backbone_pe_loopbacks,
    find_or_claim_core_interface,
    isis_net_id,
)


@pytest.mark.parametrize(
    ("loopback", "expected"),
    [
        ("10.0.0.1/32", "49.0001.0100.0000.0001.00"),
        ("10.0.0.9", "49.0001.0100.0000.0009.00"),
        ("192.168.10.200/32", "49.0001.1921.6801.0200.00"),
    ],
)
def test_isis_net_id_matches_dataset_convention(loopback: str, expected: str) -> None:
    assert isis_net_id("49.0001", loopback) == expected


def _iface(name: str) -> MagicMock:
    iface = MagicMock(save=AsyncMock())
    iface.name.value = name
    iface.role.value = "cust"
    iface.status.value = "free"
    iface.description.value = "Customer-facing (free)"
    iface.mtu.value = 1500
    return iface


@pytest.mark.asyncio
async def test_find_or_claim_returns_existing_port_without_saving() -> None:
    """An already-claimed port (matching description) is adopted as-is, not re-saved."""
    existing = _iface("Ethernet5")
    client = MagicMock(filters=AsyncMock(return_value=[existing]))

    result = await find_or_claim_core_interface(client, "pe-02", "To pe-09", "b")

    assert result is existing
    existing.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_or_claim_converts_lowest_free_port_outside_tracking() -> None:
    """With no claimed port, the lowest free port becomes a core port, saved untracked."""
    free_hi, free_lo = _iface("Ethernet7"), _iface("Ethernet6")
    client = MagicMock(filters=AsyncMock(side_effect=[[], [free_hi, free_lo]]))

    result = await find_or_claim_core_interface(client, "pe-02", "To pe-09", "b")

    assert result is free_lo
    assert free_lo.role.value == "core"
    assert free_lo.status.value == "active"
    assert free_lo.description.value == "To pe-09"
    assert free_lo.mtu.value == 9000
    free_lo.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)


@pytest.mark.asyncio
async def test_find_or_claim_raises_when_no_free_port() -> None:
    client = MagicMock(filters=AsyncMock(side_effect=[[], []]))
    with pytest.raises(RuntimeError, match="No free physical interface on pe-02"):
        await find_or_claim_core_interface(client, "pe-02", "To pe-09", "b")


def _pe_payload(name: str, device_id: str, *addresses: tuple[str, str]) -> dict:
    return {
        "node": {
            "id": device_id,
            "name": {"value": name},
            "interfaces": {
                "edges": [
                    {
                        "node": {
                            "name": {"value": "Loopback0"},
                            "ip_addresses": {
                                "edges": [
                                    {"node": {"id": ip_id, "address": {"value": address}}}
                                    for ip_id, address in addresses
                                ]
                            },
                        }
                    },
                    # A physical port comes back as an empty fragment.
                    {"node": {}},
                ]
            },
        }
    }


@pytest.mark.asyncio
async def test_backbone_pe_loopbacks_maps_name_to_ids_and_address() -> None:
    pes = [
        _pe_payload("pe-01", "d1", ("ip1", "10.0.0.1/32")),
        _pe_payload("pe-02", "d2", ("ip2", "10.0.0.2/32")),
        _pe_payload("pe-09", "d9"),  # no address yet: skipped
    ]
    payload = {"TopologyMplsBackbone": {"edges": [{"node": {"pes": {"edges": pes}}}]}}
    client = MagicMock(execute_graphql=AsyncMock(return_value=payload))

    result = await backbone_pe_loopbacks(client, "b", "bb-1")

    assert result == {
        "pe-01": ("d1", "ip1", "10.0.0.1/32"),
        "pe-02": ("d2", "ip2", "10.0.0.2/32"),
    }
    client.execute_graphql.assert_awaited_once()
    assert client.execute_graphql.await_args.kwargs["branch_name"] == "b"
    # Scoped to one backbone, so a second backbone's PEs never join the mesh.
    assert client.execute_graphql.await_args.kwargs["variables"] == {"backbone_id": "bb-1"}
