"""L3VPN generator.

Materialises VRF, route targets, PE-CE interfaces, IPs, and the
optional eBGP session for each site of a ``ServiceL3Vpn``. Idempotent.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from infrahub_sdk.generator import InfrahubGenerator

from .common import (
    allocate_prefix_from_pool,
    find_or_create_route_target,
    next_free_physical_interface,
)

LOG = logging.getLogger(__name__)


class L3VpnGenerator(InfrahubGenerator):
    """Generator that materialises everything downstream of a ServiceL3Vpn row."""

    data: dict[str, Any]

    async def generate(self, data: dict[str, Any] | None = None) -> None:
        """Generate VRF + per-site resources for a single L3VPN."""
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

        for site_edge in vpn["sites"]["edges"]:
            await self._materialise_site(site_edge["node"], vrf, vpn, backbone_as_id)

    async def _ensure_vrf(self, vpn: dict[str, Any], backbone_asn: int) -> Any:
        """Create the VRF (and its RT) if absent. Returns the VRF node."""
        vpn_id = int(vpn["vpn_id"]["value"])
        rd = f"{backbone_asn}:{vpn_id}"

        if vpn.get("vrf") and vpn["vrf"].get("node"):
            return await self.client.get(
                kind="IpamVRF",
                id=vpn["vrf"]["node"]["id"],
                branch=self.branch,
            )

        rt = await find_or_create_route_target(self.client, rd, self.branch)
        vrf = await self.client.create(
            kind="IpamVRF",
            branch=self.branch,
            name=vpn["name"]["value"],
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

    async def _materialise_site(
        self,
        site: dict[str, Any],
        vrf: Any,
        vpn: dict[str, Any],
        backbone_as_id: str,
    ) -> None:
        """Allocate interface, /30, IPs, eBGP session if needed.

        Args:
            site: Site node from the GraphQL query result.
            vrf: The IpamVRF node for this L3VPN.
            vpn: The ServiceL3Vpn node from the GraphQL query result.
            backbone_as_id: Infrahub ID of the backbone RoutingAutonomousSystem node.
        """
        site_obj = await self.client.get(
            kind="ServiceL3VpnSite",
            id=site["id"],
            branch=self.branch,
        )
        pe_name = site["pe_device"]["node"]["name"]["value"]

        if site.get("pe_interface") and site["pe_interface"].get("node"):
            iface = await self.client.get(
                kind="InterfacePhysical",
                id=site["pe_interface"]["node"]["id"],
                branch=self.branch,
            )
        else:
            iface = await next_free_physical_interface(self.client, pe_name, self.branch)
            iface.role.value = "cust"
            iface.status.value = "active"  # remove from the free-interface candidate set
            iface.description.value = f"L3VPN {vpn['name']['value']}"
            await iface.save(allow_upsert=True)
            site_obj.pe_interface = iface

        has_pe_addr = site.get("pe_address") and site["pe_address"].get("node")
        has_ce_addr = site.get("ce_address") and site["ce_address"].get("node")
        if not has_pe_addr or not has_ce_addr:
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
            pe_ip = await self.client.create(
                kind="IpamIPAddress",
                branch=self.branch,
                address=f"{net.network_address + 1}/30",
                interface=iface,
                vrf=vrf,
            )
            await pe_ip.save(allow_upsert=True)
            ce_ip = await self.client.create(
                kind="IpamIPAddress",
                branch=self.branch,
                address=f"{net.network_address + 2}/30",
                vrf=vrf,
            )
            await ce_ip.save(allow_upsert=True)

            site_obj.pe_address = pe_ip
            site_obj.ce_address = ce_ip

        cust_subnet = await self.client.get(
            kind="IpamPrefix",
            id=site["customer_subnet"]["node"]["id"],
            branch=self.branch,
        )
        cust_subnet.vrf = vrf
        await cust_subnet.save(allow_upsert=True)

        if site["routing_protocol"]["value"] == "ebgp":
            tenant_id = vpn["tenant"]["node"]["id"]
            await self._ensure_ebgp_session(
                site, site_obj, vrf, vpn["name"]["value"], backbone_as_id, tenant_id
            )

        site_obj.status.value = "active"  # type: ignore[union-attr]
        await site_obj.save(allow_upsert=True)

    async def _ensure_ebgp_session(
        self,
        site: dict[str, Any],
        site_obj: Any,
        vrf: Any,
        vpn_name: str,
        backbone_as_id: str,
        tenant_id: str,
    ) -> None:
        """Create PE-CE eBGP session if it doesn't already exist.

        Args:
            site: Site node from the GraphQL query result.
            site_obj: The live ServiceL3VpnSite Infrahub node.
            vrf: The IpamVRF node for this L3VPN.
            vpn_name: Human-readable VPN name (for the session description).
            backbone_as_id: Infrahub ID of the backbone RoutingAutonomousSystem — derived
                from the query result to avoid coupling to a hardcoded AS name.
            tenant_id: Infrahub ID of the VPN's tenant — used as the owner of the
                customer-side RoutingAutonomousSystem when one needs to be created.
        """
        desc = f"L3VPN PE-CE {vpn_name} {site['name']['value']}"
        existing = await self.client.filters(
            kind="RoutingBGPSession",
            description__value=desc,
            branch=self.branch,
        )
        if existing:
            return

        backbone_as = await self.client.get(
            kind="RoutingAutonomousSystem",
            id=backbone_as_id,
            branch=self.branch,
        )
        remote_asn = int(site["bgp_peer_asn"]["value"])
        remote_objs = await self.client.filters(
            kind="RoutingAutonomousSystem",
            asn__value=remote_asn,
            branch=self.branch,
        )
        if remote_objs:
            remote_as = remote_objs[0]
        else:
            remote_as = await self.client.create(
                kind="RoutingAutonomousSystem",
                branch=self.branch,
                name=f"customer-as-{remote_asn}",
                asn=remote_asn,
                organization={"id": tenant_id},
            )
            await remote_as.save(allow_upsert=True)

        session = await self.client.create(
            kind="RoutingBGPSession",
            branch=self.branch,
            description=desc,
            session_type="EXTERNAL",
            role="peering",
            device={"id": site["pe_device"]["node"]["id"]},
            local_as=backbone_as,
            remote_as=remote_as,
            local_ip=site_obj.pe_address,
            remote_ip=site_obj.ce_address,
            vrf=vrf,
            status="active",
        )
        await session.save(allow_upsert=True)
