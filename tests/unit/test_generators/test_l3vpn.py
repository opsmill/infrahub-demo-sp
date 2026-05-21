"""Unit tests for the L3VPN generator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_generator_creates_vrf_with_correct_rd_on_first_run() -> None:
    """First run of an intent allocates vpn_id from the band pool and creates the VRF."""
    from generators.generate_l3vpn import L3VpnGenerator

    # The generator flow on first run touches:
    #   client.get(kind="ServiceL3VpnIntent", id=...)          -> intent_obj
    #   client.get(kind="CoreNumberPool", name__value=...)     -> pool
    #   client.create(kind="ServiceL3Vpn", ...)                -> realised service
    #   client.get(kind="CoreStandardGroup", name__value="l3vpns")
    #   client.create(kind="IpamRouteTarget", ...)             -> route target
    #   client.create(kind="IpamVRF", ...)                     -> the assertion target
    #
    # We use a single AsyncMock for client.create and reach into the call
    # list to find the IpamVRF call.

    intent_obj = MagicMock()
    intent_obj.status = MagicMock()
    intent_obj.failure_message = MagicMock()
    intent_obj.save = AsyncMock()

    pool = MagicMock()
    pool.id = "pool-financial"

    service = MagicMock()
    service.id = "svc-1"
    service.vpn_id.value = 100
    service.vrf.fetch = AsyncMock()
    service.vrf.peer = None  # first run — no VRF yet
    service.save = AsyncMock()
    service.status = MagicMock()
    service.name.value = "acme-prod"

    group = MagicMock()
    group.members.fetch = AsyncMock()
    group.members.peers = []
    group.members.add = MagicMock()
    group.save = AsyncMock()

    async def get_side_effect(*, kind: str, **_: object) -> object:
        return {
            "ServiceL3VpnIntent": intent_obj,
            "CoreNumberPool": pool,
            "CoreStandardGroup": group,
        }[kind]

    client = MagicMock()
    client.get = AsyncMock(side_effect=get_side_effect)
    client.create = AsyncMock(return_value=service)
    client.filters = AsyncMock(return_value=[])

    payload = {
        "ServiceL3VpnIntent": {
            "edges": [
                {
                    "node": {
                        "id": "intent-1",
                        "name": {"value": "acme-prod"},
                        "description": {"value": ""},
                        "band": {"value": "financial"},
                        "address_family": {"value": "ipv4"},
                        "status": {"value": "draft"},
                        "failure_message": {"value": None},
                        "tenant": {"node": {"id": "t1", "name": {"value": "acme"}}},
                        "sites": {"edges": []},
                        "realised_service": {"node": None},
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

    # The IpamVRF create is the assertion target.
    vrf_calls = [c for c in client.create.await_args_list if c.kwargs.get("kind") == "IpamVRF"]
    assert vrf_calls, "Expected an IpamVRF create"
    assert vrf_calls[0].kwargs["vrf_rd"] == "65000:100"
    assert vrf_calls[0].kwargs["name"] == "acme-prod"

    # The last status the generator writes on the success path is "active".
    assert intent_obj.status.value == "active"
    # No failure on the success path.
    assert intent_obj.failure_message.value == ""
