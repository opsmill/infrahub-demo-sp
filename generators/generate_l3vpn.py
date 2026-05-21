"""L3VPN generator.

Driven by a ``ServiceL3VpnIntent``. The generator:

1. Marks the intent ``in_delivery``.
2. Allocates a ``vpn_id`` from the band-scoped CoreNumberPool.
3. Upserts the realised ``ServiceL3Vpn`` and its per-site ``ServiceL3VpnSite``.
4. Materialises VRF, route target, PE-CE /30, PE/CE IPs, and (optional) eBGP.
5. Joins the realised service to the ``l3vpns`` group so artifact pipelines pick it up.
6. Sets the intent ``status`` to ``active`` on success, or ``failed`` (with
   ``failure_message``) on hard error.

Idempotent at every step — re-running on the same intent converges to the
same realised graph.
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
    """Materialise a realised L3VPN from a ServiceL3VpnIntent."""

    data: dict[str, Any]

    async def generate(self, data: dict[str, Any] | None = None) -> None:
        """Generate VRF + per-site resources for a single L3VPN intent."""
        payload = data or self.data
        intent_edges = payload.get("ServiceL3VpnIntent", {}).get("edges", [])
        if not intent_edges:
            LOG.warning("No ServiceL3VpnIntent matched; nothing to generate")
            return
        intent = intent_edges[0]["node"]
        intent_obj = await self.client.get(
            kind="ServiceL3VpnIntent",
            id=intent["id"],
            branch=self.branch,
        )

        try:
            await self._set_status(intent_obj, "in_delivery", failure=None)

            backbone_edges = payload.get("TopologyMplsBackbone", {}).get("edges", [])
            if not backbone_edges:
                raise RuntimeError("TopologyMplsBackbone mpls-backbone-1 not found")
            backbone_node = backbone_edges[0]["node"]
            backbone_asn = int(backbone_node["asn"]["node"]["asn"]["value"])
            backbone_as_id: str = backbone_node["asn"]["node"]["id"]

            service = await self._ensure_realised_service(intent, intent_obj)
            vrf = await self._ensure_vrf(intent, service, backbone_asn)

            for site_edge in intent["sites"]["edges"]:
                await self._materialise_site(
                    site_edge["node"], service, vrf, intent, backbone_as_id
                )

            await self._set_status(intent_obj, "active", failure=None)
        except Exception as exc:  # noqa: BLE001 — surface any failure on the intent
            LOG.exception("Generator failed for intent %s", intent["name"]["value"])
            await self._set_status(intent_obj, "failed", failure=str(exc))
            raise

    async def _set_status(
        self,
        intent_obj: Any,
        status: str,
        failure: str | None,
    ) -> None:
        """Write status + failure_message back to the intent (branch-aware)."""
        intent_obj.status.value = status
        intent_obj.failure_message.value = failure or ""
        await intent_obj.save(allow_upsert=True)

    async def _ensure_realised_service(
        self,
        intent: dict[str, Any],
        intent_obj: Any,
    ) -> Any:
        """Upsert the realised ServiceL3Vpn for this intent.

        vpn_id is drawn from the band-scoped pool on first creation;
        subsequent runs reuse the existing realised service.
        """
        existing = intent.get("realised_service") or {}
        if existing.get("node"):
            return await self.client.get(
                kind="ServiceL3Vpn",
                id=existing["node"]["id"],
                branch=self.branch,
            )

        band = intent["band"]["value"]
        pool_name = f"vpn_id_pool_{band}"
        pool: Any = await self.client.get(
            kind="CoreNumberPool",
            name__value=pool_name,
            branch=self.branch,
        )

        service = await self.client.create(
            kind="ServiceL3Vpn",
            branch=self.branch,
            name=intent["name"]["value"],
            description=intent["description"].get("value") or "",
            address_family=intent["address_family"]["value"],
            status="provisioning",
            tenant={"id": intent["tenant"]["node"]["id"]},
            vpn_id={"from_pool": {"id": pool.id}},
            source_intent={"id": intent["id"]},
        )
        await service.save(allow_upsert=True)

        # Join the artifact pipeline target group.
        group: Any = await self.client.get(
            kind="CoreStandardGroup",
            name__value="l3vpns",
            branch=self.branch,
        )
        await group.members.fetch()
        if service.id not in {p.id for p in group.members.peers}:
            group.members.add(service.id)
            await group.save(allow_upsert=True)

        intent_obj.realised_service = service
        await intent_obj.save(allow_upsert=True)
        return service

    async def _ensure_vrf(
        self,
        intent: dict[str, Any],
        service: Any,
        backbone_asn: int,
    ) -> Any:
        """Create the VRF (and its RT) if absent. Returns the VRF node."""
        await service.vrf.fetch()
        if service.vrf.peer is not None:
            return service.vrf.peer

        vpn_id_value = service.vpn_id.value
        rd = f"{backbone_asn}:{int(vpn_id_value)}"

        rt = await find_or_create_route_target(self.client, rd, self.branch)
        vrf = await self.client.create(
            kind="IpamVRF",
            branch=self.branch,
            name=intent["name"]["value"],
            vrf_rd=rd,
            import_rt=rt,
            export_rt=rt,
            namespace={"hfid": ["default"]},
        )
        await vrf.save(allow_upsert=True)

        service.vrf = vrf
        service.status.value = "active"
        await service.save(allow_upsert=True)
        return vrf

    async def _materialise_site(
        self,
        intent_site: dict[str, Any],
        service: Any,
        vrf: Any,
        intent: dict[str, Any],
        backbone_as_id: str,
    ) -> None:
        """Materialise the realised ServiceL3VpnSite for one intent site."""
        site_obj = await self._ensure_realised_site(intent_site, service)
        pe_name = intent_site["pe_device"]["node"]["name"]["value"]

        await site_obj.pe_interface.fetch()
        if site_obj.pe_interface.peer is None:
            iface = await next_free_physical_interface(self.client, pe_name, self.branch)
            iface.role.value = "cust"
            iface.status.value = "active"
            iface.description.value = f"L3VPN {intent['name']['value']}"
            await iface.save(allow_upsert=True)
            site_obj.pe_interface = iface
        else:
            iface = site_obj.pe_interface.peer

        await site_obj.pe_address.fetch()
        await site_obj.ce_address.fetch()
        if site_obj.pe_address.peer is None or site_obj.ce_address.peer is None:
            p2p = await allocate_prefix_from_pool(
                self.client,
                "pe_ce_pool",
                self.branch,
                identifier=f"l3vpnsite-{site_obj.id}",
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
            id=intent_site["customer_subnet"]["node"]["id"],
            branch=self.branch,
        )
        cust_subnet.vrf = vrf
        await cust_subnet.save(allow_upsert=True)

        if intent_site["routing_protocol"]["value"] == "ebgp":
            tenant_id = intent["tenant"]["node"]["id"]
            await self._ensure_ebgp_session(
                intent_site, site_obj, vrf, intent["name"]["value"], backbone_as_id, tenant_id
            )

        site_obj.status.value = "active"
        await site_obj.save(allow_upsert=True)

    async def _ensure_realised_site(
        self,
        intent_site: dict[str, Any],
        service: Any,
    ) -> Any:
        """Upsert the realised ServiceL3VpnSite for one intent site."""
        existing = await self.client.filters(
            kind="ServiceL3VpnSite",
            l3vpn__ids=[service.id],
            name__value=intent_site["name"]["value"],
            branch=self.branch,
        )
        if existing:
            site_obj = existing[0]
        else:
            site_obj = await self.client.create(
                kind="ServiceL3VpnSite",
                branch=self.branch,
                name=intent_site["name"]["value"],
                routing_protocol=intent_site["routing_protocol"]["value"],
                status="provisioning",
                l3vpn={"id": service.id},
                pe_device={"id": intent_site["pe_device"]["node"]["id"]},
                customer_subnet={"id": intent_site["customer_subnet"]["node"]["id"]},
            )
            if intent_site["bgp_peer_asn"].get("value") is not None:
                site_obj.bgp_peer_asn.value = int(intent_site["bgp_peer_asn"]["value"])  # type: ignore[union-attr]
            if intent_site["static_routes"].get("value") is not None:
                site_obj.static_routes.value = intent_site["static_routes"]["value"]  # type: ignore[union-attr]
            await site_obj.save(allow_upsert=True)
        return site_obj

    async def _ensure_ebgp_session(
        self,
        intent_site: dict[str, Any],
        site_obj: Any,
        vrf: Any,
        vpn_name: str,
        backbone_as_id: str,
        tenant_id: str,
    ) -> None:
        """Create PE-CE eBGP session if it doesn't already exist."""
        desc = f"L3VPN PE-CE {vpn_name} {intent_site['name']['value']}"
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
        remote_asn = int(intent_site["bgp_peer_asn"]["value"])
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
            device={"id": intent_site["pe_device"]["node"]["id"]},
            local_as=backbone_as,
            remote_as=remote_as,
            local_ip=site_obj.pe_address,
            remote_ip=site_obj.ce_address,
            vrf=vrf,
            status="active",
        )
        await session.save(allow_upsert=True)
