"""Unit tests for the L3VPN generator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_generator_creates_vrf_with_correct_rd_on_first_run() -> None:
    """First run creates IpamVRF with vrf_rd = backbone_asn:vpn_id."""
    from generators.generate_l3vpn import L3VpnGenerator

    client = MagicMock()
    client.create = AsyncMock()

    # Idempotency is now read from the live ServiceL3Vpn.vrf relationship rather
    # than the query payload, so the mock must report "no VRF yet" (peer=None)
    # for the first-run path to create one.
    def _get_side_effect(**kwargs: object) -> MagicMock:
        if kwargs.get("kind") == "ServiceL3Vpn":
            obj = MagicMock(status=MagicMock(value="draft"), save=AsyncMock())
            obj.vrf = MagicMock(fetch=AsyncMock(), peer=None)
            return obj
        return MagicMock(save=AsyncMock())

    client.get = AsyncMock(side_effect=_get_side_effect)
    client.filters = AsyncMock(return_value=[])

    payload = {
        "ServiceL3Vpn": {
            "edges": [
                {
                    "node": {
                        "id": "vpn-1",
                        "name": {"value": "acme-prod"},
                        "vpn_id": {"value": 100},
                        "address_family": {"value": "ipv4"},
                        "status": {"value": "draft"},
                        "tenant": {"node": {"id": "t1", "name": {"value": "acme"}}},
                        "vrf": None,
                        "sites": {"edges": []},
                    }
                }
            ]
        },
        "TopologyMplsBackbone": {
            "edges": [{"node": {"asn": {"node": {"id": "as-65000", "asn": {"value": 65000}}}}}]
        },
    }

    gen = L3VpnGenerator.__new__(L3VpnGenerator)
    gen.client = client
    gen.data = payload
    gen.branch = "test-branch"

    await gen.generate()

    vrf_calls = [c for c in client.create.await_args_list if c.kwargs.get("kind") == "IpamVRF"]
    assert vrf_calls, "Expected an IpamVRF create"
    assert vrf_calls[0].kwargs["vrf_rd"] == "65000:100"
    assert vrf_calls[0].kwargs["name"] == "acme-prod"
