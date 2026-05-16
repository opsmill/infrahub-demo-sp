"""Unit tests for pe_interface_alloc check."""

from __future__ import annotations

import pytest

from checks.pe_interface_alloc import PeInterfaceAllocCheck


def _site(name: str, pe: str, iface_id: str | None) -> dict:
    """Build a minimal ServiceL3VpnSite edge node for testing.

    Args:
        name: Site name.
        pe: PE device name.
        iface_id: Interface node ID, or None if no interface is assigned.

    Returns:
        A dict shaped like a GraphQL edge node.
    """
    return {
        "node": {
            "name": {"value": name},
            "pe_device": {"node": {"name": {"value": pe}}},
            "pe_interface": (
                {"node": {"id": iface_id, "name": {"value": "Ethernet1"}}} if iface_id else None
            ),
        }
    }


@pytest.mark.asyncio
async def test_unique_pe_interface_passes() -> None:
    """Two sites on different PEs/interfaces produce no errors."""
    data = {
        "ServiceL3VpnSite": {
            "edges": [
                _site("a", "pe-lon-arista", "iface-1"),
                _site("b", "pe-par-nokia", "iface-2"),
            ]
        }
    }
    check = PeInterfaceAllocCheck(branch="main")
    await check.validate(data)
    assert check.errors == []


@pytest.mark.asyncio
async def test_double_claimed_interface_fails() -> None:
    """Two sites claiming the same PE interface produce one error."""
    data = {
        "ServiceL3VpnSite": {
            "edges": [
                _site("a", "pe-lon-arista", "iface-1"),
                _site("b", "pe-lon-arista", "iface-1"),
            ]
        }
    }
    check = PeInterfaceAllocCheck(branch="main")
    await check.validate(data)
    assert len(check.errors) == 1
    assert "double-claimed" in check.errors[0]["message"]


@pytest.mark.asyncio
async def test_sites_without_interface_are_ignored() -> None:
    """Sites with no interface assigned are skipped and do not cause errors."""
    data = {"ServiceL3VpnSite": {"edges": [_site("a", "pe-lon-arista", None)]}}
    check = PeInterfaceAllocCheck(branch="main")
    await check.validate(data)
    assert check.errors == []
