"""Unit tests for the L3VPN generator."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.generate_l3vpn import L3VpnGenerator


def _site(
    *,
    name: str = "trading-london",
    routing_protocol: str = "ebgp",
    bgp_peer_asn: int | None = None,
    ce_device: str | None = None,
) -> dict[str, Any]:
    """Build a site node shaped like the ``l3vpn`` query result.

    Args:
        name: Site name.
        routing_protocol: ``ebgp``, ``static``, or ``connected``.
        bgp_peer_asn: Per-site peer-AS override, or ``None`` to use the pool.
        ce_device: CE device name, or ``None`` for an unmanaged CE.

    Returns:
        A single site node dict.
    """
    node: dict[str, Any] = {
        "id": f"site-{name}",
        "name": {"value": name},
        "routing_protocol": {"value": routing_protocol},
        "bgp_peer_asn": {"value": bgp_peer_asn},
        "static_routes": {"value": []},
        "status": {"value": "provisioning"},
        "pe_device": {"node": {"id": "pe-1", "name": {"value": "pe-01"}}},
        "ce_device": {"node": None},
        "ce_interface": {"node": None},
        "customer_subnet": {"node": {"id": "prefix-1", "prefix": {"value": "10.200.10.0/24"}}},
    }
    if ce_device:
        node["ce_device"] = {"node": {"id": f"{ce_device}-id", "name": {"value": ce_device}}}
        node["ce_interface"] = {"node": {"id": f"{ce_device}-eth1", "name": {"value": "Ethernet1"}}}
    return node


def _payload(sites: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a full ``l3vpn`` query payload around the given sites."""
    return {
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
                        "sites": {"edges": [{"node": s} for s in (sites or [])]},
                    }
                }
            ]
        },
        "TopologyMplsBackbone": {
            "edges": [{"node": {"asn": {"node": {"id": "as-65000", "asn": {"value": 65000}}}}}]
        },
    }


def _generator(payload: dict[str, Any]) -> tuple[L3VpnGenerator, MagicMock]:
    """Return a generator wired to a mock client that reports "nothing exists yet".

    ``client.create`` records every call, so tests assert on what the generator
    decided to build. ``allocate_next_ip_prefix`` returns a fixed /30 so the
    PE/CE host addresses are deterministic.
    """
    client = MagicMock()
    client.create = AsyncMock(side_effect=lambda **kwargs: MagicMock(save=AsyncMock()))

    # Nothing exists yet, except one free PE port for the allocator to hand out
    # (a PE with no free port is an error the generator is right to raise on).
    free_port = MagicMock(save=AsyncMock())
    free_port.name.value = "Ethernet9"

    async def _filters(**kwargs: Any) -> list[Any]:
        if kwargs.get("status__value") == "free":
            return [free_port]
        return []

    client.filters = AsyncMock(side_effect=_filters)

    # Cache by (kind, id/name) so repeated lookups of the same node return the
    # same object — otherwise identity assertions (e.g. "the PE and CE sessions
    # reference the same backbone AS") would fail on a mock artefact.
    fetched: dict[tuple[Any, ...], MagicMock] = {}

    def _get(**kwargs: Any) -> MagicMock:
        key = (kwargs.get("kind"), kwargs.get("id"), kwargs.get("name__value"))
        if key not in fetched:
            obj = MagicMock(save=AsyncMock())
            if kwargs.get("kind") == "ServiceL3Vpn":
                obj.status = MagicMock(value="draft")
            fetched[key] = obj
        return fetched[key]

    client.get = AsyncMock(side_effect=_get)
    prefix = MagicMock(save=AsyncMock())
    prefix.prefix.value = "10.100.0.0/30"
    client.allocate_next_ip_prefix = AsyncMock(return_value=prefix)

    gen = L3VpnGenerator.__new__(L3VpnGenerator)
    gen.client = client
    gen.data = payload
    gen.branch = "test-branch"
    return gen, client


def _creates(client: MagicMock, kind: str) -> list[Any]:
    """Return the recorded ``client.create`` calls for one node kind."""
    return [c for c in client.create.await_args_list if c.kwargs.get("kind") == kind]


@pytest.mark.asyncio
async def test_generator_creates_vrf_with_correct_rd_on_first_run() -> None:
    """First run creates IpamVRF with vrf_rd = backbone_asn:vpn_id."""
    gen, client = _generator(_payload())
    await gen.generate()

    vrf_calls = _creates(client, "IpamVRF")
    assert vrf_calls, "Expected an IpamVRF create"
    assert vrf_calls[0].kwargs["vrf_rd"] == "65000:100"
    assert vrf_calls[0].kwargs["name"] == "acme-prod"


@pytest.mark.asyncio
async def test_customer_asn_is_allocated_from_the_pool() -> None:
    """With no site override, the customer AS number comes from the pool.

    The pool node itself is handed to the `asn` attribute — that is what makes
    the server allocate and track the value, rather than the generator picking
    a number.
    """
    gen, client = _generator(_payload([_site()]))
    await gen.generate()

    as_calls = _creates(client, "RoutingAutonomousSystem")
    assert len(as_calls) == 1, "Expected exactly one customer AS for the VPN"
    assert as_calls[0].kwargs["name"] == "customer-as-acme-prod"
    pool_lookups = [
        c for c in client.get.await_args_list if c.kwargs.get("kind") == "CoreNumberPool"
    ]
    assert [c.kwargs["name__value"] for c in pool_lookups] == ["customer_asn_pool"]
    assert as_calls[0].kwargs["asn"] is not None
    assert not isinstance(as_calls[0].kwargs["asn"], int), (
        "asn must be the pool node, not a hardcoded number"
    )


@pytest.mark.asyncio
async def test_all_sites_of_a_vpn_share_one_customer_as() -> None:
    """Two sites, one customer AS — a customer is a single routing domain."""
    gen, client = _generator(_payload([_site(name="trading-london"), _site(name="trading-zurich")]))
    await gen.generate()

    assert len(_creates(client, "RoutingAutonomousSystem")) == 1


@pytest.mark.asyncio
async def test_site_asn_override_bypasses_the_pool() -> None:
    """An explicit bgp_peer_asn peers with that AS and never touches the pool."""
    gen, client = _generator(_payload([_site(bgp_peer_asn=65501)]))
    await gen.generate()

    as_calls = _creates(client, "RoutingAutonomousSystem")
    assert [c.kwargs["asn"] for c in as_calls] == [65501]
    assert not [c for c in client.get.await_args_list if c.kwargs.get("kind") == "CoreNumberPool"]


@pytest.mark.asyncio
async def test_non_ebgp_vpn_allocates_no_customer_as() -> None:
    """A VPN with no eBGP site must not burn an ASN from the pool."""
    gen, client = _generator(_payload([_site(routing_protocol="connected")]))
    await gen.generate()

    assert not _creates(client, "RoutingAutonomousSystem")


@pytest.mark.asyncio
async def test_managed_ce_gets_its_own_ebgp_session_mirroring_the_pe() -> None:
    """A site with a CE builds both ends of the peering.

    The CE session is the mirror image of the PE's: local/remote AS and
    local/remote IP swapped. Only the PE side existing would leave the lab CE
    with no BGP at all.
    """
    gen, client = _generator(_payload([_site(ce_device="ce-trading-lon")]))
    await gen.generate()

    sessions = {c.kwargs["description"]: c.kwargs for c in _creates(client, "RoutingBGPSession")}
    pe_side = sessions["L3VPN PE-CE acme-prod trading-london"]
    ce_side = sessions["L3VPN CE-PE acme-prod trading-london"]

    assert pe_side["device"] == {"id": "pe-1"}
    assert ce_side["device"] == {"id": "ce-trading-lon-id"}
    assert pe_side["local_ip"] is ce_side["remote_ip"]
    assert pe_side["remote_ip"] is ce_side["local_ip"]
    assert pe_side["local_as"] is ce_side["remote_as"]
    assert pe_side["remote_as"] is ce_side["local_as"]
    # The VRF is a PE-side construct; the CE is not VPN-aware.
    assert "vrf" in pe_side
    assert "vrf" not in ce_side


@pytest.mark.asyncio
async def test_unmanaged_ce_gets_only_the_pe_side_session() -> None:
    """No ce_device means nothing to configure on the customer side."""
    gen, client = _generator(_payload([_site()]))
    await gen.generate()

    descriptions = [c.kwargs["description"] for c in _creates(client, "RoutingBGPSession")]
    assert descriptions == ["L3VPN PE-CE acme-prod trading-london"]


@pytest.mark.asyncio
async def test_pe_interface_is_matched_by_description_before_allocating() -> None:
    """A pre-wired PE port is reused rather than a free one being allocated.

    This is what keeps the hand-drawn PE-CE wiring intact: the generator looks
    for an interface already described as this VPN's port and binds to it.
    """
    gen, client = _generator(_payload([_site()]))
    prewired = MagicMock(save=AsyncMock())
    prewired.name.value = "Ethernet3"

    async def _filters(**kwargs: Any) -> list[Any]:
        if kwargs.get("kind") == "InterfacePhysical":
            assert kwargs["description__value"] == "L3VPN acme-prod"
            return [prewired]
        return []

    client.filters = AsyncMock(side_effect=_filters)
    await gen.generate()

    assert prewired.status.value == "active", "pre-wired port must leave the free pool"
    assert prewired.role.value == "cust"


@pytest.mark.asyncio
async def test_second_run_touches_every_object_it_owns() -> None:
    """A re-run must re-save everything it owns, or tracking deletes it.

    The SDK runs generators inside
    `start_tracking(..., delete_unused_nodes=True)`: any node a previous run
    created and this run does not touch is reaped as orphaned. Early-returning
    on "it already exists" is what made `invoke bootstrap` destructive on a
    populated database — VRFs, customer ASNs, PE addresses and PE-CE sessions
    were all deleted, while the CE address and PE interface survived only
    because their code paths happened to re-save.

    Simulates the second run: every lookup finds an existing object, so the
    generator creates nothing and must instead have saved each one.
    """
    gen, client = _generator(_payload([_site(ce_device="ce-trading-lon")]))

    owned: dict[str, MagicMock] = {}

    async def _filters(**kwargs: Any) -> list[Any]:
        kind = kwargs.get("kind")
        if kwargs.get("status__value") == "free":
            return []
        if kind in (
            "IpamVRF",
            "RoutingAutonomousSystem",
            "IpamIPAddress",
            "RoutingBGPSession",
            "InterfacePhysical",
        ):
            key = f"{kind}:{sorted(kwargs.items())!r}"
            owned.setdefault(key, MagicMock(save=AsyncMock()))
            return [owned[key]]
        return []

    client.filters = AsyncMock(side_effect=_filters)
    await gen.generate()

    assert owned, "expected the second run to find pre-existing objects"
    untouched = [k.split(":")[0] for k, m in owned.items() if not m.save.await_count]
    assert not untouched, f"these existing objects were never re-saved: {sorted(set(untouched))}"
    assert not _creates(client, "IpamVRF"), "second run should not recreate the VRF"
    assert not _creates(client, "RoutingBGPSession"), "second run should not recreate sessions"
