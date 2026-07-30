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
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from infrahub_sdk.generator import InfrahubGenerator

from .common import (
    allocate_asn_from_pool,
    allocate_prefix_from_pool,
    allocate_vlan_subinterface,
    find_or_create_route_target,
    next_free_physical_interface,
)

LOG = logging.getLogger(__name__)

CUSTOMER_ASN_POOL = "customer_asn_pool"


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

        vrf = await self._ensure_vrf(vpn, backbone_asn)
        customer_as = await self._ensure_customer_as(vpn)

        for site_edge in vpn["sites"]["edges"]:
            await self._materialise_site(site_edge["node"], vrf, vpn, backbone_as_id, customer_as)

    async def _ensure_vrf(self, vpn: dict[str, Any], backbone_asn: int) -> Any:
        """Create the VRF (and its RT) if absent. Returns the VRF node."""
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
        if existing_vrf:
            vrf = existing_vrf[0]
            await vrf.save(allow_upsert=True)  # touch: keep it out of the reaper
        else:
            rt = await find_or_create_route_target(self.client, rd, self.branch)
            vrf = await self.client.create(
                kind="IpamVRF",
                branch=self.branch,
                name=vpn_name,
                vrf_rd=rd,
                import_rt=rt,
                export_rt=rt,
                namespace={"hfid": ["default"]},
            )
            await vrf.save(allow_upsert=True)

        vpn_obj = await self.client.get(kind="ServiceL3Vpn", id=vpn["id"], branch=self.branch)
        vpn_obj.vrf = vrf
        vpn_obj.status.value = "active"  # type: ignore[union-attr]
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

        vpn_name = vpn["name"]["value"]
        as_name = f"customer-as-{vpn_name}"
        existing = await self.client.filters(
            kind="RoutingAutonomousSystem", name__value=as_name, branch=self.branch
        )
        if existing:
            customer_as = existing[0]
            await customer_as.save(allow_upsert=True)  # touch: see module docstring
        else:
            customer_as = await allocate_asn_from_pool(
                self.client,
                CUSTOMER_ASN_POOL,
                self.branch,
                name=as_name,
                organization_id=vpn["tenant"]["node"]["id"],
                description=f"Customer AS for L3VPN {vpn_name} (PE-CE eBGP).",
            )

        vpn_obj = await self.client.get(kind="ServiceL3Vpn", id=vpn["id"], branch=self.branch)
        vpn_obj.customer_asn = customer_as
        await vpn_obj.save(allow_upsert=True)
        return customer_as

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

        await self._ensure_private_vlan(site, vpn)

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

    async def _ensure_ip_address(self, address: str, vrf: Any, iface: Any | None) -> Any:
        """Return the IpamIPAddress for ``address``, creating it if absent.

        Args:
            address: The address in CIDR notation.
            vrf: The IpamVRF to bind the address to.
            iface: Interface to attach the address to, or ``None`` to leave it
                unattached (the CE side is attached later, once the CE port is
                known).

        Returns:
            The IpamIPAddress node.
        """
        existing = await self.client.filters(
            kind="IpamIPAddress", address__value=address, branch=self.branch
        )
        if existing:
            ip_address = existing[0]
            await ip_address.save(allow_upsert=True)  # touch: see module docstring
            return ip_address
        payload: dict[str, Any] = {"address": address, "vrf": vrf}
        if iface is not None:
            payload["interface"] = iface
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
        existing = await self.client.filters(
            kind="RoutingAutonomousSystem", asn__value=remote_asn, branch=self.branch
        )
        if existing:
            override_as = existing[0]
            await override_as.save(allow_upsert=True)  # touch: see module docstring
            return override_as
        remote_as = await self.client.create(
            kind="RoutingAutonomousSystem",
            branch=self.branch,
            name=f"customer-as-{remote_asn}",
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
        desc = f"L3VPN PE-CE {vpn_name} {site['name']['value']}"
        existing = await self.client.filters(
            kind="RoutingBGPSession",
            description__value=desc,
            branch=self.branch,
        )
        if existing:
            await existing[0].save(allow_upsert=True)  # touch: see module docstring
            return

        backbone_as = await self.client.get(
            kind="RoutingAutonomousSystem",
            id=backbone_as_id,
            branch=self.branch,
        )
        session = await self.client.create(
            kind="RoutingBGPSession",
            branch=self.branch,
            description=desc,
            session_type="EXTERNAL",
            role="peering",
            device={"id": site["pe_device"]["node"]["id"]},
            local_as=backbone_as,
            remote_as=remote_as,
            local_ip=pe_ip,
            remote_ip=ce_ip,
            vrf=vrf,
            status="active",
        )
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
            ce_iface.description.value = f"To {site['pe_device']['node']['name']['value']}"
            await ce_iface.save(allow_upsert=True)
            ce_ip.interface = ce_iface
            await ce_ip.save(allow_upsert=True)

        # The CE belongs to the customer's AS; recording it on the device keeps
        # the CE config transform from having to infer it from a session.
        ce_device = await self.client.get(
            kind="DcimDevice", id=ce_device_node["id"], branch=self.branch
        )
        ce_device.asn = remote_as
        await ce_device.save(allow_upsert=True)

        desc = f"L3VPN CE-PE {vpn_name} {site['name']['value']}"
        existing = await self.client.filters(
            kind="RoutingBGPSession", description__value=desc, branch=self.branch
        )
        if existing:
            await existing[0].save(allow_upsert=True)  # touch: see module docstring
            return

        backbone_as = await self.client.get(
            kind="RoutingAutonomousSystem", id=backbone_as_id, branch=self.branch
        )
        session = await self.client.create(
            kind="RoutingBGPSession",
            branch=self.branch,
            description=desc,
            session_type="EXTERNAL",
            role="peering",
            device={"id": ce_device_node["id"]},
            local_as=remote_as,
            remote_as=backbone_as,
            local_ip=ce_ip,
            remote_ip=pe_ip,
            status="active",
        )
        await session.save(allow_upsert=True)

    async def _ensure_private_vlan(self, site: dict[str, Any], vpn: dict[str, Any]) -> None:
        """Put a dot1q sub-interface carrying the customer's VLAN on the CE.

        The VLAN comes from the pool the VPN names in ``vlan_pool`` — one pool
        per customer, so their ranges stay separate. The sub-interface carries
        the customer LAN gateway (first usable address of the site's
        ``customer_subnet``), which is why the parent port holds no address.

        Skipped when the site names no ``ce_private_interface`` or the VPN names
        no ``vlan_pool``: an unmanaged CE has no private side for us to configure.

        Idempotency is by parent: on a re-run the existing sub-interface is found
        and re-saved rather than reallocated, so the VLAN is stable and the
        tracking reaper leaves it alone (see the module docstring).

        Args:
            site: Site node from the GraphQL query result.
            vpn: The ServiceL3Vpn node from the GraphQL query result.
        """
        parent_node = (site.get("ce_private_interface") or {}).get("node")
        pool_node = (vpn.get("vlan_pool") or {}).get("node")
        if not parent_node or not pool_node:
            return

        parent: Any = await self.client.get(
            kind="InterfacePhysical", id=parent_node["id"], branch=self.branch
        )
        device_name = site["ce_device"]["node"]["name"]["value"]
        parent_name = parent_node["name"]["value"]

        # Find an existing sub-interface of this parent. Looking it up by name is
        # circular — the name embeds the VLAN we have not allocated yet — so
        # match on the parent instead.
        existing = await self.client.filters(
            kind="InterfaceVirtual",
            device__name__value=device_name,
            parent_interface__ids=[parent_node["id"]],
            branch=self.branch,
        )
        description = f"{parent.description.value} (customer VLAN)"
        sub: Any
        if existing:
            sub = existing[0]
            await sub.save(allow_upsert=True)  # touch: see module docstring
        else:
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
        existing_ip = await self.client.filters(
            kind="IpamIPAddress", address__value=gateway, branch=self.branch
        )
        gateway_ip: Any
        if existing_ip:
            gateway_ip = existing_ip[0]
        else:
            gateway_ip = await self.client.create(
                kind="IpamIPAddress", branch=self.branch, address=gateway
            )
        gateway_ip.interface = sub
        gateway_ip.description.value = f"{device_name} customer LAN gateway"
        await gateway_ip.save(allow_upsert=True)
