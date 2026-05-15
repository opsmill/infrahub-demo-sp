"""Unit test for l3vpn_overlap check."""

from __future__ import annotations

import pytest

from checks.l3vpn_overlap import L3VpnOverlapCheck


def _vpn_node(id_: str, name: str, vpn_id: int, rd: str) -> dict:
    return {
        "node": {
            "id": id_,
            "name": {"value": name},
            "vpn_id": {"value": vpn_id},
            "vrf": {"node": {"vrf_rd": {"value": rd}}},
        }
    }


@pytest.mark.asyncio
async def test_no_overlap_passes() -> None:
    data = {
        "ServiceL3Vpn": {
            "edges": [
                _vpn_node("1", "a", 100, "65000:100"),
                _vpn_node("2", "b", 101, "65000:101"),
            ]
        }
    }
    check = L3VpnOverlapCheck.__new__(L3VpnOverlapCheck)
    result = await check.validate(data)
    assert result == []


@pytest.mark.asyncio
async def test_duplicate_rd_fails() -> None:
    data = {
        "ServiceL3Vpn": {
            "edges": [
                _vpn_node("1", "a", 100, "65000:100"),
                _vpn_node("2", "b", 101, "65000:100"),
            ]
        }
    }
    check = L3VpnOverlapCheck.__new__(L3VpnOverlapCheck)
    result = await check.validate(data)
    assert any("duplicate RD" in m for m in result)
