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
    ce_private_interface: str | None = None,
    customer_subnet: str = "10.200.10.0/24",
    namespace: tuple[str, str] | None = ("ns-1", "vrf-acme-prod"),
) -> dict[str, Any]:
    """Build a site node shaped like the ``l3vpn`` query result.

    Args:
        name: Site name.
        routing_protocol: ``ebgp``, ``static``, or ``connected``.
        bgp_peer_asn: Per-site peer-AS override, or ``None`` to use the pool.
        ce_device: CE device name, or ``None`` for an unmanaged CE.
        ce_private_interface: CE LAN-facing port name, or ``None`` when the site
            has no private side for the generator to configure.
        customer_subnet: The site's customer LAN prefix.
        namespace: ``(id, name)`` of the IP namespace the customer prefix lives
            in, or ``None`` to model a prefix created without one (which the
            query returns as a null relationship, meaning ``default``).

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
        "ce_private_interface": {"node": None},
        "customer_subnet": {
            "node": {
                "id": "prefix-1",
                "prefix": {"value": customer_subnet},
                "ip_namespace": (
                    {"node": {"id": namespace[0], "name": {"value": namespace[1]}}}
                    if namespace
                    else {"node": None}
                ),
            }
        },
    }
    if ce_device:
        node["ce_device"] = {"node": {"id": f"{ce_device}-id", "name": {"value": ce_device}}}
        node["ce_interface"] = {"node": {"id": f"{ce_device}-eth1", "name": {"value": "Ethernet1"}}}
    if ce_private_interface:
        node["ce_private_interface"] = {
            "node": {
                "id": f"{ce_device}-{ce_private_interface}",
                "name": {"value": ce_private_interface},
            }
        }
    return node


def _payload(
    sites: list[dict[str, Any]] | None = None, *, vlan_pool: str | None = None
) -> dict[str, Any]:
    """Build a full ``l3vpn`` query payload around the given sites.

    Args:
        sites: Site nodes from :func:`_site`.
        vlan_pool: Name of the VPN's private-side VLAN pool, or ``None`` when the
            VPN names none (which makes the generator skip the private side).

    Returns:
        A payload shaped like the ``l3vpn`` query result.
    """
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
                        "vlan_pool": (
                            {"node": {"id": "pool-1", "name": {"value": vlan_pool}}}
                            if vlan_pool
                            else {"node": None}
                        ),
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
        """Return the free PE port for the status=free lookup, else nothing."""
        if kwargs.get("status__value") == "free":
            return [free_port]
        return []

    client.filters = AsyncMock(side_effect=_filters)

    # Cache by (kind, id/name) so repeated lookups of the same node return the
    # same object — otherwise identity assertions (e.g. "the PE and CE sessions
    # reference the same backbone AS") would fail on a mock artefact.
    fetched: dict[tuple[Any, ...], MagicMock] = {}

    def _get(**kwargs: Any) -> MagicMock:
        """Return a cached mock per (kind, id, name) so identity holds."""
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


def _with_ce_device(client: MagicMock, *, existing_as_id: str | None) -> MagicMock:
    """Make ``client.get`` return a CE whose ``asn`` peer is already known.

    Args:
        client: The mock client from :func:`_generator`.
        existing_as_id: Infrahub id of the AS already on the device, or ``None``
            for a device with no AS yet.

    Returns:
        The CE device mock, so the test can assert what the generator did to it.
    """
    device = MagicMock(save=AsyncMock())
    device.asn = None if existing_as_id is None else MagicMock(id=existing_as_id)
    passthrough = client.get.side_effect

    def _get(**kwargs: Any) -> MagicMock:
        """Return the CE device for DcimDevice lookups, else the default mock."""
        if kwargs.get("kind") == "DcimDevice":
            return device
        return passthrough(**kwargs)

    client.get = AsyncMock(side_effect=_get)
    return device


def _ce_session(client: MagicMock) -> dict[str, Any]:
    """Return the kwargs of the CE-side eBGP session the generator created."""
    return next(
        c.kwargs
        for c in _creates(client, "RoutingBGPSession")
        if str(c.kwargs.get("description", "")).startswith("L3VPN CE-PE")
    )


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
        """Return the pre-wired port, asserting it is looked up by description."""
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
        """Return nothing: model a database where no object exists yet."""
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


@pytest.mark.asyncio
async def test_route_target_is_ensured_even_when_the_vrf_already_exists() -> None:
    """The RT must be touched on every run, not only when creating the VRF.

    Reaching `find_or_create_route_target` only through the create branch left
    the RT untouched whenever the VRF already existed, so the tracking reaper
    deleted it. `vrf.import_rt` went null and the PE config artifact then failed
    to render with `UndefinedError: 'None' has no attribute 'name'` — observed on
    pe-01 and pe-08 after a re-bootstrap.
    """
    gen, client = _generator(_payload())
    existing_vrf = MagicMock(save=AsyncMock())

    async def _filters(**kwargs: Any) -> list[Any]:
        """Return the pre-existing objects, so the run adopts instead of creating."""
        if kwargs.get("kind") == "IpamVRF":
            return [existing_vrf]
        if kwargs.get("status__value") == "free":
            return []
        return []

    client.filters = AsyncMock(side_effect=_filters)
    await gen.generate()

    assert _creates(client, "IpamRouteTarget"), (
        "the route target must be ensured on the VRF-reuse path too"
    )
    # Re-bound on both sides, so a VRF that already lost its RT is repaired.
    assert existing_vrf.import_rt is not None
    assert existing_vrf.export_rt is not None
    assert existing_vrf.save.await_count >= 1


@pytest.mark.asyncio
async def test_private_vlan_subinterface_is_allocated_from_the_vpn_pool() -> None:
    """The CE private side gets a dot1q sub-interface off the named pool.

    The VLAN has to come from the VPN's own `vlan_pool` (one pool per customer),
    the sub-interface hangs off the site's `ce_private_interface`, and the LAN
    gateway — first usable address of the customer subnet — lands on the
    sub-interface rather than the parent port.
    """
    gen, client = _generator(
        _payload(
            [_site(ce_device="ce-trading-lon", ce_private_interface="Ethernet2")],
            vlan_pool="vlan_pool_markets_trading",
        )
    )
    await gen.generate()

    subs = _creates(client, "InterfaceVirtual")
    assert len(subs) == 1, "expected exactly one sub-interface for the private side"
    pool_lookups = [
        c.kwargs["name__value"]
        for c in client.get.await_args_list
        if c.kwargs.get("kind") == "CoreNumberPool"
    ]
    assert "vlan_pool_markets_trading" in pool_lookups
    # The pool node itself is the attribute value — that is what allocates.
    assert not isinstance(subs[0].kwargs["dot1q_id"], int)

    gateways = [
        c.kwargs["address"]
        for c in _creates(client, "IpamIPAddress")
        if c.kwargs["address"] == "10.200.10.1/24"
    ]
    assert gateways == ["10.200.10.1/24"], "LAN gateway must be .1 of the customer subnet"


@pytest.mark.asyncio
async def test_no_private_vlan_without_a_pool_or_a_private_port() -> None:
    """No `vlan_pool` or no `ce_private_interface` means no sub-interface.

    An unmanaged CE has no private side for us to configure, and allocating a
    VLAN with nowhere to put it would burn a pool entry for nothing.
    """
    gen, client = _generator(
        _payload([_site(ce_device="ce-trading-lon", ce_private_interface="Ethernet2")])
    )
    await gen.generate()
    assert not _creates(client, "InterfaceVirtual")

    gen, client = _generator(
        _payload([_site(ce_device="ce-trading-lon")], vlan_pool="vlan_pool_markets_trading")
    )
    await gen.generate()
    assert not _creates(client, "InterfaceVirtual")


@pytest.mark.asyncio
async def test_pending_subinterface_name_is_repaired_on_the_next_run() -> None:
    """An interrupted allocation leaves `<parent>.pending`; the re-run fixes it.

    `allocate_vlan_subinterface` creates the sub-interface under a placeholder
    name and renames it once the pool has assigned a VLAN. If that second save
    never landed, the CE template rendered `interface Ethernet2.pending`, which
    EOS rejects. The parent-keyed lookup is the only place that can notice.
    """
    gen, client = _generator(
        _payload(
            [_site(ce_device="ce-trading-lon", ce_private_interface="Ethernet2")],
            vlan_pool="vlan_pool_markets_trading",
        )
    )
    stale = MagicMock(save=AsyncMock())
    stale.name.value = "Ethernet2.pending"
    stale.dot1q_id.value = 137
    free_port = MagicMock(save=AsyncMock())
    free_port.name.value = "Ethernet9"

    async def _filters(**kwargs: Any) -> list[Any]:
        """Report the half-finished sub-interface, plus a free PE port."""
        if kwargs.get("kind") == "InterfaceVirtual":
            return [stale]
        if kwargs.get("status__value") == "free":
            return [free_port]
        return []

    client.filters = AsyncMock(side_effect=_filters)
    await gen.generate()

    assert stale.name.value == "Ethernet2.137"
    assert not _creates(client, "InterfaceVirtual"), "must adopt, not reallocate a VLAN"
    stale.save.assert_awaited()


@pytest.mark.asyncio
async def test_lan_gateway_on_another_interface_is_refused() -> None:
    """A gateway address already on a different interface is never re-pointed.

    Two *VPNs* no longer meet here — each customer's space has its own namespace
    — but two sites of one VPN naming the same customer_subnet still collide
    inside it. Ownership is decided by the interface the address sits on rather
    than by its description, so an address with an empty or foreign description
    cannot slip through and get quietly stolen.
    """
    gen, client = _generator(
        _payload(
            [_site(ce_device="ce-ib-lon", ce_private_interface="Ethernet2")],
            vlan_pool="vlan_pool_investment_banking",
        )
    )
    foreign = MagicMock(save=AsyncMock())
    foreign.description.value = ""  # nothing says who owns it
    foreign.interface.id = "somebody-elses-subinterface"
    free_port = MagicMock(save=AsyncMock())
    free_port.name.value = "Ethernet9"

    async def _filters(**kwargs: Any) -> list[Any]:
        """Only the foreign LAN gateway already exists, plus a free PE port."""
        if kwargs.get("address__value") == "10.200.10.1/24":
            return [foreign]
        if kwargs.get("status__value") == "free":
            return [free_port]
        return []

    client.filters = AsyncMock(side_effect=_filters)
    with pytest.raises(RuntimeError, match="already attached to another interface"):
        await gen.generate()


@pytest.mark.asyncio
async def test_lan_gateway_already_on_this_subinterface_is_adopted() -> None:
    """This site's own gateway from a previous run is reused, not refused."""
    gen, client = _generator(
        _payload(
            [_site(ce_device="ce-ib-lon", ce_private_interface="Ethernet2")],
            vlan_pool="vlan_pool_investment_banking",
        )
    )
    own_sub = MagicMock(save=AsyncMock(), id="sub-1")
    own_sub.name.value = "Ethernet2.150"
    own_sub.dot1q_id.value = 150
    gateway = MagicMock(save=AsyncMock())
    gateway.description.value = ""
    gateway.interface.id = "sub-1"
    free_port = MagicMock(save=AsyncMock())
    free_port.name.value = "Ethernet9"

    async def _filters(**kwargs: Any) -> list[Any]:
        """This site's sub-interface and its gateway both already exist."""
        if kwargs.get("kind") == "InterfaceVirtual":
            return [own_sub]
        if kwargs.get("address__value") == "10.200.10.1/24":
            return [gateway]
        if kwargs.get("status__value") == "free":
            return [free_port]
        return []

    client.filters = AsyncMock(side_effect=_filters)
    await gen.generate()

    assert gateway.description.value == "ce-ib-lon customer LAN gateway"
    gateway.save.assert_awaited()


@pytest.mark.asyncio
async def test_customer_space_lands_in_the_prefix_namespace() -> None:
    """The VRF and the LAN gateway join the customer prefix's namespace.

    Customer prefixes are private space two customers may both pick, and
    IpamIPAddress/IpamPrefix are unique on [value, ip_namespace] — so customer
    space is namespaced per VPN while provider space (the PE-CE /30 from
    pe_ce_pool) stays in ``default``.
    """
    gen, client = _generator(
        _payload(
            [_site(ce_device="ce-ib-lon", ce_private_interface="Ethernet2")],
            vlan_pool="vlan_pool_investment_banking",
        )
    )
    await gen.generate()

    namespaced = [c.kwargs for c in _creates(client, "IpamIPAddress") if "ip_namespace" in c.kwargs]
    assert len(namespaced) == 1, "only the customer LAN gateway is namespaced"
    assert namespaced[0]["address"] == "10.200.10.1/24"
    assert namespaced[0]["ip_namespace"] == {"id": "ns-1"}

    # The VRF joins the same namespace — not the hardcoded `default` it used to
    # get, which is what made two customers' rows collide.
    assert _creates(client, "IpamVRF")[0].kwargs["namespace"] == {"id": "ns-1"}

    provider = [
        c.kwargs for c in _creates(client, "IpamIPAddress") if "ip_namespace" not in c.kwargs
    ]
    assert sorted(c["address"] for c in provider) == ["10.100.0.1/30", "10.100.0.2/30"]


@pytest.mark.asyncio
async def test_generator_never_writes_an_ip_namespace() -> None:
    """The namespace is read, never created or re-saved.

    Generators run under ``delete_unused_nodes=True``, so any node they save
    joins their tracking group — and a later run that does not save it makes the
    reaper delete it. For a namespace still holding customer prefixes that delete
    fails, and the whole generator run dies on an unreadable
    ``IpamNamespaceDelete`` GraphQL error. Seen on a live server; the fix is to
    never write the node at all.
    """
    gen, client = _generator(
        _payload(
            [_site(ce_device="ce-ib-lon", ce_private_interface="Ethernet2")],
            vlan_pool="vlan_pool_investment_banking",
        )
    )
    await gen.generate()

    assert not _creates(client, "IpamNamespace"), "the namespace must not be created here"
    assert not any(
        c.kwargs.get("kind") == "IpamNamespace" for c in client.filters.await_args_list
    ), "not even looked up by name — it is read off the customer prefix"


@pytest.mark.asyncio
async def test_prefix_without_a_namespace_falls_back_to_default() -> None:
    """A customer prefix created by hand lands in `default`, and that still works.

    Isolation comes from creating the prefix in a per-VPN namespace, which the
    datasets and the catalog do. A VPN whose prefix was made in the UI simply
    gets the old flat behaviour instead of an error.
    """
    gen, client = _generator(
        _payload(
            [_site(ce_device="ce-ib-lon", ce_private_interface="Ethernet2", namespace=None)],
            vlan_pool="vlan_pool_investment_banking",
        )
    )
    await gen.generate()

    assert _creates(client, "IpamVRF")[0].kwargs["namespace"] == {"hfid": ["default"]}
    assert not any("ip_namespace" in c.kwargs for c in _creates(client, "IpamIPAddress"))
    gateway_lookups = [
        c.kwargs
        for c in client.filters.await_args_list
        if c.kwargs.get("address__value") == "10.200.10.1/24"
    ]
    assert gateway_lookups
    assert all(k.get("ip_namespace__name__value") == "default" for k in gateway_lookups)


@pytest.mark.asyncio
async def test_address_lookups_are_scoped_to_a_namespace() -> None:
    """Every address lookup names a namespace, or it could match another customer."""
    gen, client = _generator(
        _payload(
            [_site(ce_device="ce-ib-lon", ce_private_interface="Ethernet2")],
            vlan_pool="vlan_pool_investment_banking",
        )
    )
    await gen.generate()

    gateway_lookups = [
        c.kwargs
        for c in client.filters.await_args_list
        if c.kwargs.get("address__value") == "10.200.10.1/24"
    ]
    assert gateway_lookups, "expected a LAN gateway lookup"
    assert all("ip_namespace__ids" in k for k in gateway_lookups)

    p2p_lookups = [
        c.kwargs
        for c in client.filters.await_args_list
        if str(c.kwargs.get("address__value", "")).endswith("/30")
    ]
    assert p2p_lookups, "expected PE-CE /30 lookups"
    assert all(k.get("ip_namespace__name__value") == "default" for k in p2p_lookups)


@pytest.mark.asyncio
async def test_ce_device_as_is_claimed_when_the_device_has_none() -> None:
    """The first site that needs it writes the customer AS onto the CE."""
    gen, client = _generator(_payload([_site(ce_device="ce-shared")]))
    device = _with_ce_device(client, existing_as_id=None)
    await gen.generate()

    assert device.asn is _ce_session(client)["local_as"]
    device.save.assert_awaited()


@pytest.mark.asyncio
async def test_ce_device_as_is_not_overwritten_by_a_second_vpn() -> None:
    """A CE already in another customer's AS keeps it; the session carries ours.

    DcimDevice.asn is cardinality one, so a CE terminating sites of two L3VPNs
    cannot hold both. Overwriting it let whichever site ran last win, and the
    rendered `router bgp <asn>` then claimed that AS for both peerings — the
    other session came up in the wrong AS and never established.
    """
    gen, client = _generator(_payload([_site(ce_device="ce-shared")]))
    device = _with_ce_device(client, existing_as_id="as-first-customer")
    await gen.generate()

    assert device.asn.id == "as-first-customer", "must not be re-pointed"
    assert device.asn is not _ce_session(client)["local_as"]


@pytest.mark.asyncio
async def test_subinterface_is_keyed_per_site_not_per_port() -> None:
    """The sub-interface lookup names the site, so a shared CE port holds one each.

    Keying on the parent port alone made a second site adopt the first site's
    sub-interface: it kept the first customer's VLAN, its own vlan_pool was never
    consumed, and both gateways ended up in one broadcast domain.
    """
    gen, client = _generator(
        _payload(
            [_site(name="ib-london", ce_device="ce-shared", ce_private_interface="Ethernet2")],
            vlan_pool="vlan_pool_investment_banking",
        )
    )
    await gen.generate()

    vlan_lookups = [
        c.kwargs
        for c in client.filters.await_args_list
        if c.kwargs.get("kind") == "InterfaceVirtual"
    ]
    assert vlan_lookups, "expected a sub-interface lookup"
    assert vlan_lookups[0]["description__value"] == "L3VPN acme-prod ib-london customer VLAN"
    assert vlan_lookups[0]["parent_interface__ids"] == ["ce-shared-Ethernet2"]
