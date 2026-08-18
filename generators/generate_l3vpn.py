"""L3VPN generator.

Materialises VRF, route targets, the customer ASN, PE-CE interfaces, IPs, and
the eBGP sessions on both ends of each site of a ``ServiceL3Vpn``. Idempotent.

GENERATOR TRACKING: the SDK runs `generate()` inside
`start_tracking(..., delete_unused_nodes=True)`. Any node a previous run created
that this run does not *touch* is treated as unused and deleted. So every object
this generator owns must be re-saved on every run, even when it already exists
and needs no change — a bare "found it, return early" silently orphans it. That
is what made `invoke bootstrap` destructive on a populated database: VRFs,
customer ASNs, PE addresses and PE-CE sessions were all reaped, while the CE
address and PE interface survived precisely because their code paths re-saved
them.

CONCURRENT RUNS: several runs of this generator are routinely in flight for the
same service. Creating each site and adding the VPN to the `l3vpns` group each
emit an event, and every one of them dispatches this generator — a two-site
Service Catalog request produces three runs within milliseconds of each other
(objects/events/00_triggers.yml explains why both rules exist). Every
find-or-create here therefore has to tolerate losing the race to a sibling run
rather than aborting the flow; see `_allocate_customer_as` for the one case
`allow_upsert` cannot cover.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from infrahub_sdk.exceptions import GraphQLError
from infrahub_sdk.generator import InfrahubGenerator

from .common import (
    DEFAULT_IP_NAMESPACE,
    allocate_asn_from_pool,
    allocate_prefix_from_pool,
    allocate_vlan_subinterface,
    find_or_create_route_target,
    find_vlan_subinterface,
    next_free_physical_interface,
    touch,
)

LOG = logging.getLogger(__name__)

CUSTOMER_ASN_POOL = "customer_asn_pool"


def _customer_namespace(site: dict[str, Any]) -> tuple[str | None, str]:
    """Return the IP namespace of a site's customer prefix.

    The customer's address space is whichever namespace their ``customer_subnet``
    was created in — ``vrf-<vpn name>`` when the datasets or the Service Catalog
    made it, ``default`` for a prefix created by hand. Reading it here rather
    than deriving the name keeps the generator out of the business of owning it:
    see the note on DEFAULT_IP_NAMESPACE in generators/common.py for what
    happened when it did.

    Args:
        site: Site node from the GraphQL query result.

    Returns:
        A ``(namespace_id, namespace_name)`` tuple. The id is ``None`` when the
        query returned no namespace, in which case the name is ``default`` and
        callers fall back to filtering by that name.
    """
    subnet = (site.get("customer_subnet") or {}).get("node") or {}
    namespace = (subnet.get("ip_namespace") or {}).get("node") or {}
    name = (namespace.get("name") or {}).get("value") or DEFAULT_IP_NAMESPACE
    return namespace.get("id"), name


def _namespace_filter(namespace_id: str | None) -> dict[str, Any]:
    """Return the IPAM filter scoping a lookup to one customer's namespace.

    Every address lookup here has to be namespace-scoped: the same address
    string exists in as many namespaces as there are customers using that
    private range, and IpamIPAddress is unique on [address__value,
    ip_namespace]. An unscoped lookup returns whichever row it finds first,
    which may be another customer's.

    Args:
        namespace_id: Infrahub id of the customer namespace, or ``None`` for the
            provider-owned ``default`` namespace.

    Returns:
        Keyword arguments to pass to ``client.filters``.
    """
    if namespace_id:
        return {"ip_namespace__ids": [namespace_id]}
    return {"ip_namespace__name__value": DEFAULT_IP_NAMESPACE}


class L3VpnGenerator(InfrahubGenerator):
    """Generator that materialises everything downstream of a ServiceL3Vpn row."""

    data: dict[str, Any]

    async def generate(self, data: dict[str, Any] | None = None) -> None:
        """Generate VRF, customer ASN, and per-site resources for a single L3VPN."""
        payload = data or self.data
        vpn_edges = payload.get("ServiceL3Vpn", {}).get("edges", [])
        if not vpn_edges:
            LOG.warning("No ServiceL3Vpn matched; nothing to generate")
            return
        vpn = vpn_edges[0]["node"]

        backbone_edges = payload.get("TopologyMplsBackbone", {}).get("edges", [])
        if not backbone_edges:
            raise RuntimeError("TopologyMplsBackbone mpls-backbone-1 not found")
        backbone_node = backbone_edges[0]["node"]
        backbone_asn = int(backbone_node["asn"]["node"]["asn"]["value"])
        backbone_as_id: str = backbone_node["asn"]["node"]["id"]

        # The VRF is bound to the namespace the customer's prefixes live in.
        # Taken from the first site because the VRF is per VPN, while each site's
        # own gateway is placed in that site's prefix namespace — so a VPN whose
        # sites somehow span namespaces still produces correct per-site
        # addressing.
        sites = [edge["node"] for edge in vpn["sites"]["edges"]]
        vrf_ns_id = _customer_namespace(sites[0])[0] if sites else None
        # Reset per run: the PE port key is per-VPN, so two sites of this VPN
        # landing on one PE would otherwise share a port. See _ensure_pe_interface.
        self._claimed_pe_ports: set[tuple[str, str]] = set()

        # Customer AS first, so the single ServiceL3Vpn write below carries it
        # along with the VRF and status. Fetching and saving the VPN node once
        # per concern meant two round trips for one row.
        customer_as = await self._ensure_customer_as(vpn)
        vrf = await self._ensure_vrf(vpn, backbone_asn, vrf_ns_id, customer_as)

        for site in sites:
            await self._materialise_site(site, vrf, vpn, backbone_as_id, customer_as)

    async def _ensure_vrf(
        self,
        vpn: dict[str, Any],
        backbone_asn: int,
        namespace_id: str | None,
        customer_as: Any | None,
    ) -> Any:
        """Create the VRF (and its RT) if absent. Returns the VRF node.

        Also writes the ServiceL3Vpn row — vrf, status and customer_asn together,
        because they are one save on one node.

        Args:
            vpn: The ServiceL3Vpn node from the GraphQL query result.
            backbone_asn: The backbone AS number, the left half of the RD/RT.
            namespace_id: Infrahub id of the namespace holding this customer's
                address space, or ``None`` to bind the VRF to ``default``.
            customer_as: The pool-allocated customer AS, or ``None`` when this
                VPN needs none.
        """
        vpn_id = int(vpn["vpn_id"]["value"])
        rd = f"{backbone_asn}:{vpn_id}"

        # Idempotency is derived from deterministic keys via ``client.filters``,
        # NOT the generator query: the query must not return ``vrf`` (an object
        # this generator creates), or its query-group bookkeeping destabilises in
        # the proposed-change pipeline (CoreGraphQLQueryGroupUpsert ->
        # NodeNotFound, branch wiped). See queries/service/l3vpn.gql.
        vpn_name = vpn["name"]["value"]
        existing_vrf = await self.client.filters(
            kind="IpamVRF", name__value=vpn_name, branch=self.branch
        )
        # Ensure the route target on EVERY run, not just when creating the VRF.
        # Reaching it only through the create branch left it untouched whenever
        # the VRF already existed, so the reaper deleted it: `vrf.import_rt` went
        # null and the PE template then died on `import_rt.node.name`. Re-binding
        # it here also repairs a VRF that already lost its RT that way.
        rt = await find_or_create_route_target(self.client, rd, self.branch)
        # Re-bound on every run for the same reason the RD and RTs are: setting it
        # only on create left an adopted VRF pointing at whatever it had, which
        # for every VRF made before this convention was the shared `default`.
        # Referenced by id, never saved as a node — the generator does not own
        # the namespace (see generators/common.py).
        namespace_ref: dict[str, Any] = (
            {"id": namespace_id} if namespace_id else {"hfid": [DEFAULT_IP_NAMESPACE]}
        )
        if existing_vrf:
            vrf = existing_vrf[0]
            # Re-assert the RD for the same reason the RTs are re-asserted: it is
            # derived from backbone_asn:vpn_id, so setting it only on create left
            # an adopted VRF with a stale RD (or, since `vrf_rd` is optional, none
            # at all — the PE template then rendered a literal `rd None`).
            vrf.vrf_rd.value = rd  # type: ignore[union-attr]
            vrf.import_rt = rt
            vrf.export_rt = rt
            vrf.namespace = namespace_ref
            await vrf.save(allow_upsert=True)
        else:
            vrf = await self.client.create(
                kind="IpamVRF",
                branch=self.branch,
                name=vpn_name,
                vrf_rd=rd,
                import_rt=rt,
                export_rt=rt,
                namespace=namespace_ref,
            )
            await vrf.save(allow_upsert=True)

        vpn_obj = await self.client.get(kind="ServiceL3Vpn", id=vpn["id"], branch=self.branch)
        vpn_obj.vrf = vrf
        vpn_obj.status.value = "active"  # type: ignore[union-attr]
        if customer_as is not None:
            vpn_obj.customer_asn = customer_as
        await vpn_obj.save(allow_upsert=True)
        return vrf

    async def _ensure_customer_as(self, vpn: dict[str, Any]) -> Any | None:
        """Return the VPN's customer AS, allocating one from the pool if needed.

        One AS is shared by every eBGP site of the VPN — that is what makes the
        customer a single routing domain across its sites. The ASN itself comes
        from ``customer_asn_pool``, so customer AS numbers are never hand-picked.

        Idempotency comes from the AS name (``customer-as-<vpn>``) rather than
        the generator query: ``customer_asn`` is an object this generator
        creates, so querying it would destabilise the query group in the
        proposed-change pipeline — same constraint as ``vrf`` in
        :meth:`_ensure_vrf`.

        Args:
            vpn: The ServiceL3Vpn node from the GraphQL query result.

        Returns:
            The RoutingAutonomousSystem node, or ``None`` when nothing needs one
            — no eBGP site, or every eBGP site carries its own
            ``bgp_peer_asn``. Allocating in that case would silently consume a
            pool ASN that nothing ever peers with.
        """
        sites = [edge["node"] for edge in vpn["sites"]["edges"]]
        if not any(
            site["routing_protocol"]["value"] == "ebgp"
            and (site.get("bgp_peer_asn") or {}).get("value") is None
            for site in sites
        ):
            return None

        as_name = f"customer-as-{vpn['name']['value']}"
        existing = await self.client.filters(
            kind="RoutingAutonomousSystem", name__value=as_name, branch=self.branch
        )
        # The link onto ServiceL3Vpn.customer_asn is written by _ensure_vrf,
        # which already has that row open.
        return (
            await touch(existing[0]) if existing else await self._allocate_customer_as(vpn, as_name)
        )

    async def _allocate_customer_as(self, vpn: dict[str, Any], as_name: str) -> Any:
        """Allocate the VPN's customer AS, adopting one a concurrent run just made.

        The lookup-then-create in :meth:`_ensure_customer_as` is not atomic, and
        more than one generator run is routinely in flight for the same service:
        creating each site and adding the VPN to the ``l3vpns`` group each emit
        an event, and every one of them dispatches this generator (see
        objects/events/00_triggers.yml). Two runs can therefore both see no AS
        and both try to create one.

        ``allow_upsert`` cannot resolve that — ``RoutingAutonomousSystem``'s HFID
        is ``[asn__value, name__value]`` and the pool issues a fresh ``asn`` on
        every attempt, so the HFID never matches and the server rejects the
        combination outright (see generators/common.py:allocate_asn_from_pool).
        The loser of the race has to adopt the winner's row instead.

        Re-reading after the failure is what distinguishes the two cases: an AS
        that now exists under this name proves a sibling run created it, so
        adopting it is correct and the run continues. Nothing under that name
        means the create failed for some other reason, and that must not be
        swallowed — losing it here is what turned a real error into an
        L3VPN with a VRF but no PE-CE addressing and no eBGP sessions.

        Args:
            vpn: The ServiceL3Vpn node from the GraphQL query result.
            as_name: Name to create the AS under — its natural key.

        Returns:
            The RoutingAutonomousSystem node, whether this run made it or adopted
            it from a concurrent run.

        Raises:
            GraphQLError: If the create failed for any reason other than a
                concurrent run having already created this AS.
        """
        vpn_name = vpn["name"]["value"]
        try:
            return await allocate_asn_from_pool(
                self.client,
                CUSTOMER_ASN_POOL,
                self.branch,
                name=as_name,
                organization_id=vpn["tenant"]["node"]["id"],
                description=f"Customer AS for L3VPN {vpn_name} (PE-CE eBGP).",
            )
        except GraphQLError:
            raced = await self.client.filters(
                kind="RoutingAutonomousSystem", name__value=as_name, branch=self.branch
            )
            if not raced:
                raise
            LOG.info("Adopted customer AS %s created by a concurrent run", as_name)
            return await touch(raced[0])

    async def _materialise_site(
        self,
        site: dict[str, Any],
        vrf: Any,
        vpn: dict[str, Any],
        backbone_as_id: str,
        customer_as: Any | None,
    ) -> None:
        """Allocate interface, /30, IPs, CE binding, and eBGP sessions.

        Args:
            site: Site node from the GraphQL query result.
            vrf: The IpamVRF node for this L3VPN.
            vpn: The ServiceL3Vpn node from the GraphQL query result.
            backbone_as_id: Infrahub ID of the backbone RoutingAutonomousSystem node.
            customer_as: The VPN's customer AS, or ``None`` for non-eBGP VPNs.
        """
        site_obj = await self.client.get(
            kind="ServiceL3VpnSite",
            id=site["id"],
            branch=self.branch,
        )
        iface = await self._ensure_pe_interface(site, vpn)
        site_obj.pe_interface = iface

        pe_ip, ce_ip = await self._allocate_pe_ce_addressing(site, vrf, iface)
        site_obj.pe_address = pe_ip
        site_obj.ce_address = ce_ip

        cust_subnet = await self.client.get(
            kind="IpamPrefix",
            id=site["customer_subnet"]["node"]["id"],
            branch=self.branch,
        )
        cust_subnet.vrf = vrf
        await cust_subnet.save(allow_upsert=True)

        await self._ensure_private_vlan(site, vpn, vrf)

        if site["routing_protocol"]["value"] == "ebgp":
            remote_as = await self._resolve_site_peer_as(site, vpn, customer_as)
            await self._ensure_ebgp_session(
                site, vrf, vpn["name"]["value"], backbone_as_id, remote_as, pe_ip, ce_ip
            )
            await self._bind_ce_side(
                site, vpn["name"]["value"], backbone_as_id, remote_as, pe_ip, ce_ip
            )

        site_obj.status.value = "active"  # type: ignore[union-attr]
        await site_obj.save(allow_upsert=True)

    async def _ensure_pe_interface(self, site: dict[str, Any], vpn: dict[str, Any]) -> Any:
        """Return the PE port for this site, allocating a free one if needed.

        The interface description (``L3VPN <vpn name>``) is the deterministic
        key. Pre-provisioned PE-CE ports are seeded with exactly that
        description, so a hand-wired topology binds to the drawn port instead of
        having one allocated; Service Catalog requests, which seed nothing, fall
        through to the free-interface allocator.

        Idempotency comes from that key rather than the generator query — the
        query must not return ``pe_interface``, which this generator writes.
        See queries/service/l3vpn.gql and :meth:`_ensure_vrf`.

        Args:
            site: Site node from the GraphQL query result.
            vpn: The ServiceL3Vpn node from the GraphQL query result.

        Returns:
            The InterfacePhysical node bound to this site.
        """
        pe_name = site["pe_device"]["node"]["name"]["value"]
        iface_desc = f"L3VPN {vpn['name']['value']}"
        existing_iface = await self.client.filters(
            kind="InterfacePhysical",
            device__name__value=pe_name,
            description__value=iface_desc,
            branch=self.branch,
        )
        iface: Any
        if existing_iface:
            iface = existing_iface[0]
        else:
            iface = await next_free_physical_interface(self.client, pe_name, self.branch)
            iface.description.value = iface_desc

        # This key is per-VPN, deliberately: the datasets seed a hand-drawn PE-CE
        # port with exactly this description so the generator binds to the drawn
        # cable instead of allocating the next free one. The cost is that two
        # sites of the SAME VPN on the SAME PE both match it — site two would
        # adopt site one's port and `_ensure_ip_address` would re-point the /30,
        # leaving site one's eBGP session sourced from an address that no longer
        # exists on any interface. The wizard's duplicate-PE validator and the
        # pe_interface_alloc check both catch that, but neither runs when
        # scripts/run_generator.py is pointed at API-created data, so refuse it
        # here too rather than shipping a broken config silently.
        claim = (pe_name, str(iface.name.value))
        if claim in self._claimed_pe_ports:
            raise RuntimeError(
                f"Two sites of L3VPN {vpn['name']['value']} both resolve to "
                f"{pe_name}:{iface.name.value}. A PE-CE port carries one site, so give the "
                f"second site a different PE, or seed a second port described "
                f"{iface_desc!r} on {pe_name}."
            )
        self._claimed_pe_ports.add(claim)

        iface.role.value = "cust"
        iface.status.value = "active"  # remove from the free-interface candidate set
        await iface.save(allow_upsert=True)
        return iface

    async def _allocate_pe_ce_addressing(
        self, site: dict[str, Any], vrf: Any, iface: Any
    ) -> tuple[Any, Any]:
        """Allocate the PE-CE /30 and return its (pe_ip, ce_ip) address nodes.

        The /30 is allocated from ``pe_ce_pool`` under a per-site identifier, so
        re-running the generator reuses the same prefix; the two host addresses
        are then keyed by their literal value.

        Args:
            site: Site node from the GraphQL query result.
            vrf: The IpamVRF node for this L3VPN.
            iface: The PE interface the PE-side address attaches to.

        Returns:
            A ``(pe_ip, ce_ip)`` tuple of IpamIPAddress nodes.
        """
        p2p = await allocate_prefix_from_pool(
            self.client,
            "pe_ce_pool",
            self.branch,
            identifier=f"l3vpnsite-{site['id']}",
            prefix_length=30,
        )
        p2p.vrf = vrf
        await p2p.save(allow_upsert=True)

        net = ipaddress.IPv4Network(p2p.prefix.value)
        pe_ip = await self._ensure_ip_address(f"{net.network_address + 1}/30", vrf, iface)
        ce_ip = await self._ensure_ip_address(f"{net.network_address + 2}/30", vrf, None)
        return pe_ip, ce_ip

    async def _ensure_ip_address(
        self, address: str, vrf: Any, iface: Any | None, namespace_id: str | None = None
    ) -> Any:
        """Return the IpamIPAddress for ``address``, creating it if absent.

        Args:
            address: The address in CIDR notation.
            vrf: The IpamVRF to bind the address to.
            iface: Interface to attach the address to, or ``None`` to leave it
                unattached (the CE side is attached later, once the CE port is
                known).
            namespace_id: Infrahub id of the namespace the address belongs to, or
                ``None`` for the ``default`` namespace, which holds
                provider-owned space.

        Returns:
            The IpamIPAddress node.
        """
        # Both the lookup and the create are namespace-scoped. The same address
        # string exists in as many namespaces as there are customers using that
        # private range — that is what namespaces are for — and IpamIPAddress is
        # unique on [address__value, ip_namespace]. An unscoped lookup returns
        # whichever row it finds first, so it could hand back another customer's
        # address and re-point it at this interface.
        ns_filter = _namespace_filter(namespace_id)
        existing = await self.client.filters(
            kind="IpamIPAddress", address__value=address, branch=self.branch, **ns_filter
        )
        if existing:
            ip_address = existing[0]
            # Re-assert the VRF and interface for the same reason the RD and RTs
            # are re-asserted in _ensure_vrf: binding them only on create meant an
            # address adopted from a previous run kept whatever it had. The /30 is
            # allocated per site identifier, so it survives a change of PE port —
            # and stayed attached to the old port, leaving the new interface with
            # no address and PE-CE eBGP unable to come up.
            ip_address.vrf = vrf
            if iface is not None:
                ip_address.interface = iface
            await ip_address.save(allow_upsert=True)  # touch: see module docstring
            return ip_address
        payload: dict[str, Any] = {"address": address, "vrf": vrf}
        if iface is not None:
            payload["interface"] = iface
        if namespace_id:
            payload["ip_namespace"] = {"id": namespace_id}
        ip_address = await self.client.create(kind="IpamIPAddress", branch=self.branch, **payload)
        await ip_address.save(allow_upsert=True)
        return ip_address

    async def _resolve_site_peer_as(
        self, site: dict[str, Any], vpn: dict[str, Any], customer_as: Any | None
    ) -> Any:
        """Return the RoutingAutonomousSystem to peer with on this site.

        The VPN's pool-allocated ``customer_asn`` is the default. A site may
        override it with an explicit ``bgp_peer_asn`` — used by datasets and
        Service Catalog requests that must peer with a pre-agreed customer AS
        rather than one issued from the pool.

        Args:
            site: Site node from the GraphQL query result.
            vpn: The ServiceL3Vpn node from the GraphQL query result.
            customer_as: The VPN's pool-allocated customer AS, if any.

        Returns:
            The RoutingAutonomousSystem node for the remote (customer) side.

        Raises:
            RuntimeError: If the site has neither an override nor a customer AS.
        """
        override = (site.get("bgp_peer_asn") or {}).get("value")
        if override is None:
            if customer_as is None:
                raise RuntimeError(
                    f"eBGP site {site['name']['value']} has no bgp_peer_asn and the VPN "
                    f"{vpn['name']['value']} has no customer ASN"
                )
            return customer_as

        remote_asn = int(override)
        owned_name = f"customer-as-{remote_asn}"
        existing = await self.client.filters(
            kind="RoutingAutonomousSystem", asn__value=remote_asn, branch=self.branch
        )
        if existing:
            adopted = existing[0]
            # Touch ONLY an AS this generator created. Saving a node enrolls it in
            # the run's tracking group, and `delete_unused_nodes=True` makes the
            # reaper delete any previous member a later run does not save again —
            # so touching a row we merely referenced hands the reaper something
            # that was never ours.
            #
            # Nothing stops an override naming an AS that already exists for other
            # reasons: `bgp_peer_asn: 65000` is the obvious provider-AS guess and
            # would adopt Backbone-AS, which every PE's `device.asn` and the whole
            # iBGP mesh point at. Enrolled and then dropped, its delete either
            # strips the backbone or fails and takes the run down with it — the
            # same shape as the IpamNamespace hazard in generators/common.py.
            # `l3vpn_peer_asn_range` now rejects that override outright; this is
            # the belt to its braces.
            if str(getattr(adopted.name, "value", "")) == owned_name:
                return await touch(adopted)
            LOG.info(
                "Site peer AS %s resolves to pre-existing %r; adopting read-only",
                remote_asn,
                getattr(adopted.name, "value", "?"),
            )
            return adopted
        remote_as = await self.client.create(
            kind="RoutingAutonomousSystem",
            branch=self.branch,
            name=owned_name,
            asn=remote_asn,
            organization={"id": vpn["tenant"]["node"]["id"]},
        )
        await remote_as.save(allow_upsert=True)
        return remote_as

    async def _ensure_ebgp_session(
        self,
        site: dict[str, Any],
        vrf: Any,
        vpn_name: str,
        backbone_as_id: str,
        remote_as: Any,
        pe_ip: Any,
        ce_ip: Any,
    ) -> None:
        """Create the PE-side PE-CE eBGP session if it doesn't already exist.

        Args:
            site: Site node from the GraphQL query result.
            vrf: The IpamVRF node for this L3VPN.
            vpn_name: Human-readable VPN name (for the session description).
            backbone_as_id: Infrahub ID of the backbone RoutingAutonomousSystem — derived
                from the query result to avoid coupling to a hardcoded AS name.
            remote_as: The customer-side RoutingAutonomousSystem node.
            pe_ip: The PE-side IpamIPAddress of the PE-CE /30.
            ce_ip: The CE-side IpamIPAddress of the PE-CE /30.
        """
        await self._ensure_bgp_session(
            description=f"L3VPN PE-CE {vpn_name} {site['name']['value']}",
            device_id=site["pe_device"]["node"]["id"],
            local_as={"id": backbone_as_id},
            remote_as=remote_as,
            local_ip=pe_ip,
            remote_ip=ce_ip,
            vrf=vrf,
        )

    async def _ensure_bgp_session(
        self,
        *,
        description: str,
        device_id: str,
        local_as: Any,
        remote_as: Any,
        local_ip: Any,
        remote_ip: Any,
        vrf: Any | None = None,
    ) -> None:
        """Create one end of a PE-CE eBGP session, or re-assert an existing one.

        The two ends are the same node with its endpoints swapped — the PE side
        carries the VRF, the CE side does not, because a CE is not VPN-aware —
        so both go through here. They were separate near-identical copies, which
        meant a fix to one end silently missed the other.

        Adoption RE-ASSERTS the AS and address relationships rather than merely
        touching the row. The description is this session's key and embeds no
        ASN, so a site whose ``bgp_peer_asn`` changed still matches its old
        session: a touch-only adopt left the modelled session on the previous AS
        while the PE artifact rendered the new one from the site attribute, and
        the peering could never establish. The VRF and IP adopt paths in this
        module already re-assert for the same reason.

        Args:
            description: The session description, which is its natural key
                alongside the device.
            device_id: Infrahub id of the router holding this end.
            local_as: AS node (or ``{"id": ...}`` reference) for this end.
            remote_as: AS node (or reference) for the far end.
            local_ip: This end's IpamIPAddress of the PE-CE /30.
            remote_ip: The far end's IpamIPAddress.
            vrf: The IpamVRF to bind, or ``None`` for the CE side.
        """
        # Scoped by device, because that is what keys the node: RoutingBGPSession
        # is unique on [device, description__value] (schemas/extensions/
        # routing_bgp/bgp.yml), so a description alone no longer identifies one
        # session and an unscoped lookup could adopt another router's.
        existing = await self.client.filters(
            kind="RoutingBGPSession",
            description__value=description,
            device__ids=[device_id],
            branch=self.branch,
        )
        if existing:
            session: Any = existing[0]
            session.local_as = local_as
            session.remote_as = remote_as
            session.local_ip = local_ip
            session.remote_ip = remote_ip
            if vrf is not None:
                session.vrf = vrf
            await session.save(allow_upsert=True)
            return

        fields: dict[str, Any] = {
            "description": description,
            "session_type": "EXTERNAL",
            "role": "peering",
            "device": {"id": device_id},
            "local_as": local_as,
            "remote_as": remote_as,
            "local_ip": local_ip,
            "remote_ip": remote_ip,
            "status": "active",
        }
        if vrf is not None:
            fields["vrf"] = vrf
        session = await self.client.create(kind="RoutingBGPSession", branch=self.branch, **fields)
        await session.save(allow_upsert=True)

    async def _bind_ce_side(
        self,
        site: dict[str, Any],
        vpn_name: str,
        backbone_as_id: str,
        remote_as: Any,
        pe_ip: Any,
        ce_ip: Any,
    ) -> None:
        """Attach the CE address and build the CE-side eBGP session.

        Skipped when the site has no ``ce_device`` — an unmanaged CE is still a
        valid site, it just has no configuration Infrahub owns.

        Args:
            site: Site node from the GraphQL query result.
            vpn_name: Human-readable VPN name (for the session description).
            backbone_as_id: Infrahub ID of the backbone RoutingAutonomousSystem.
            remote_as: The customer-side RoutingAutonomousSystem node.
            pe_ip: The PE-side IpamIPAddress of the PE-CE /30.
            ce_ip: The CE-side IpamIPAddress of the PE-CE /30.
        """
        ce_device_node = (site.get("ce_device") or {}).get("node")
        if not ce_device_node:
            return

        ce_iface_node = (site.get("ce_interface") or {}).get("node")
        if ce_iface_node:
            ce_iface: Any = await self.client.get(
                kind="InterfacePhysical", id=ce_iface_node["id"], branch=self.branch
            )
            # Only describe the port if nothing already does. The datasets seed a
            # richer description that names the far-end port ("To pe-01
            # Ethernet3"); overwriting it with "To pe-01" every run discarded
            # that detail permanently and made the rendered CE config drift from
            # the checked-in data.
            if not ce_iface.description.value:
                ce_iface.description.value = f"To {site['pe_device']['node']['name']['value']}"
            await ce_iface.save(allow_upsert=True)
            ce_ip.interface = ce_iface
            await ce_ip.save(allow_upsert=True)

        # The CE belongs to the customer's AS; recording it on the device gives
        # the CE config transform the AS for its single `router bgp` instance.
        #
        # `DcimDevice.asn` is cardinality one, so it holds exactly one AS while a
        # CE terminating sites of two L3VPNs has two. Assigning unconditionally
        # meant whichever site the generator processed last won, and the rendered
        # `router bgp <asn>` then claimed that AS for both peerings — the other
        # session came up with the wrong local AS and never established. So the
        # first site to need it claims it, and any site whose AS differs is
        # carried per neighbour instead: the CE template reads
        # RoutingBGPSession.local_as and emits `local-as` for the odd ones out.
        ce_device = await self.client.get(
            kind="DcimDevice", id=ce_device_node["id"], branch=self.branch
        )
        current_as_id = getattr(ce_device.asn, "id", None)
        if current_as_id in (None, remote_as.id):
            ce_device.asn = remote_as
            await ce_device.save(allow_upsert=True)
        else:
            LOG.info(
                "CE %s already carries a different AS; leaving it and relying on the "
                "per-session local AS for L3VPN %s",
                ce_device_node["name"]["value"],
                vpn_name,
            )
            await touch(ce_device)

        # The mirror image of the PE side: same node, endpoints swapped, and no
        # VRF because a CE is not VPN-aware.
        await self._ensure_bgp_session(
            description=f"L3VPN CE-PE {vpn_name} {site['name']['value']}",
            device_id=ce_device_node["id"],
            local_as=remote_as,
            remote_as={"id": backbone_as_id},
            local_ip=ce_ip,
            remote_ip=pe_ip,
        )

    async def _is_own_subinterface(self, interface_id: str | None, description: str) -> bool:
        """Return whether an interface is a previous sub-interface of this site.

        The sub-interface lookup is scoped to the current parent port, so
        repointing a site's ``ce_private_interface`` (Ethernet2 to Ethernet3)
        does not find the old sub — a new one is allocated while the LAN gateway
        is still attached to the old. The ownership guard then saw a foreign
        interface and raised "two sites share the customer subnet" on a
        single-site VPN. Worse, the run aborted before the reaper could remove
        the stale sub, so every later run failed the same way: a permanent wedge
        that only a manual detach could clear.

        The site's own description identifies its previous sub, so the gateway
        can simply be re-pointed — the migration the surrounding code intends.

        Args:
            interface_id: Infrahub id the gateway is currently attached to.
            description: This site's sub-interface description.

        Returns:
            True when the interface is this site's, under any parent port.
        """
        if not interface_id:
            return False
        owned = await self.client.filters(
            kind="InterfaceVirtual",
            ids=[interface_id],
            description__value=description,
            branch=self.branch,
        )
        return bool(owned)

    async def _ensure_private_vlan(
        self, site: dict[str, Any], vpn: dict[str, Any], vrf: Any
    ) -> None:
        """Put a dot1q sub-interface carrying the customer's VLAN on the CE.

        The VLAN comes from the pool the VPN names in ``vlan_pool`` — one pool
        per customer, so their ranges stay separate. The sub-interface carries
        the customer LAN gateway (first usable address of the site's
        ``customer_subnet``), which is why the parent port holds no address.

        Skipped when the site names no ``ce_private_interface`` or the VPN names
        no ``vlan_pool``: an unmanaged CE has no private side for us to configure.

        Idempotency is by site: on a re-run this site's existing sub-interface is
        found and re-saved rather than reallocated, so the VLAN is stable and the
        tracking reaper leaves it alone (see the module docstring). A database
        written before the key became per-site has one sub-interface described
        the old way; the first run after this change allocates the site's own and
        the reaper removes the stale one.

        Args:
            site: Site node from the GraphQL query result.
            vpn: The ServiceL3Vpn node from the GraphQL query result.
            vrf: The IpamVRF node for this L3VPN, bound to the LAN gateway address
                so it is scoped the same way the PE-CE addresses are.
        """
        parent_node = (site.get("ce_private_interface") or {}).get("node")
        pool_node = (vpn.get("vlan_pool") or {}).get("node")
        # `ce_device` is independently optional from `ce_private_interface`, so it
        # has to be guarded the same way `_bind_ce_side` guards it — reaching
        # through it unconditionally raised TypeError mid-site, after the PE port
        # had been claimed but before `site_obj` was saved.
        ce_device_node = (site.get("ce_device") or {}).get("node")
        if not parent_node or not pool_node or not ce_device_node:
            return

        device_name = ce_device_node["name"]["value"]
        parent_name = parent_node["name"]["value"]

        # Find this site's sub-interface on the parent. Looking it up by name is
        # circular — the name embeds the VLAN we have not allocated yet — so the
        # description is the key, and it names the site.
        #
        # Keying on the parent alone was wrong once a CE port carries the LAN
        # side of two services: the second site adopted the first site's
        # sub-interface, so it kept the first customer's VLAN, its own vlan_pool
        # was never consumed, and both customers' gateways ended up in one
        # broadcast domain. Each site needs its own VLAN because each has its own
        # customer_subnet and therefore its own gateway.
        description = f"L3VPN {vpn['name']['value']} {site['name']['value']} customer VLAN"
        existing_sub = await find_vlan_subinterface(
            self.client,
            self.branch,
            device_name=device_name,
            parent_id=parent_node["id"],
            description=description,
        )
        sub: Any
        if existing_sub is not None:
            sub = existing_sub
            # Repair a placeholder left behind by an interrupted allocation. The
            # sub-interface is created as `<parent>.pending` and renamed once the
            # pool has assigned a VLAN (see allocate_vlan_subinterface); if that
            # second save never landed, the name stayed `.pending` and the CE
            # template rendered `interface Ethernet2.pending`, which EOS rejects.
            # This lookup matches on the parent, not the name, so it is the only
            # place that can notice and fix it.
            #
            # `is not None`, not truthiness: dot1q_id has no schema minimum, so
            # VLAN 0 is representable and would otherwise read as "unallocated",
            # leaving the name stuck at `.pending`.
            vlan_id = getattr(sub.dot1q_id, "value", None)
            if str(sub.name.value).endswith(".pending") and vlan_id is not None:
                sub.name.value = f"{parent_name}.{int(vlan_id)}"
            await sub.save(allow_upsert=True)  # touch: see module docstring
        else:
            # Fetched only here: the adopt path above needs nothing from the
            # parent node itself, so fetching it unconditionally was a round trip
            # spent on every idempotent re-run.
            parent: Any = await self.client.get(
                kind="InterfacePhysical", id=parent_node["id"], branch=self.branch
            )
            sub = await allocate_vlan_subinterface(
                self.client,
                self.branch,
                pool_name=pool_node["name"]["value"],
                parent=parent,
                device_name=device_name,
                parent_name=parent_name,
                description=description,
            )

        # The LAN gateway lives on the sub-interface, not the parent. Assigning
        # `interface` on every run also migrates it off the parent port for
        # anyone whose database predates the sub-interface layout.
        subnet = ipaddress.IPv4Network(site["customer_subnet"]["node"]["prefix"]["value"])
        gateway = f"{subnet.network_address + 1}/{subnet.prefixlen}"
        own_description = f"{device_name} customer LAN gateway"

        # The gateway goes in the same namespace as the prefix it belongs to, so
        # two L3VPNs holding the identical customer subnet never meet here —
        # IpamIPAddress is unique on [address__value, ip_namespace].
        #
        # What is left to guard is a collision *within* one namespace: two sites
        # of the same VPN whose customer_subnet is the same prefix (a data error
        # the l3vpn_site_subnet check reports). Ownership is decided by which
        # interface the address is on, not by its description — an address with an
        # empty or foreign description used to slip through the description test
        # and get silently re-pointed, taking the other site's gateway with it.
        namespace_id, namespace_name = _customer_namespace(site)
        ns_filter = _namespace_filter(namespace_id)
        existing_ip = await self.client.filters(
            kind="IpamIPAddress", address__value=gateway, branch=self.branch, **ns_filter
        )
        if existing_ip:
            attached_to = getattr(existing_ip[0].interface, "id", None)
            if attached_to not in (None, sub.id) and not await self._is_own_subinterface(
                attached_to, description
            ):
                claimed_by = str(getattr(existing_ip[0].description, "value", "") or "?")
                raise RuntimeError(
                    f"LAN gateway {gateway} in namespace {namespace_name} is already "
                    f"attached to another interface ({claimed_by}). Two sites of L3VPN "
                    f"{vpn['name']['value']} share the customer subnet {subnet}; give "
                    f"them distinct prefixes."
                )

        # _ensure_ip_address already binds `interface` to `sub` and saves; only
        # the description still needs writing.
        gateway_ip = await self._ensure_ip_address(gateway, vrf, sub, namespace_id=namespace_id)
        gateway_ip.description.value = own_description
        await gateway_ip.save(allow_upsert=True)
