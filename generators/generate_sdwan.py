"""SD-WAN generator.

Driven by a ``ServiceSdwanIntent``. The generator:

1. Marks the intent ``in_delivery``.
2. Allocates a ``service_id`` from the band-scoped CoreNumberPool.
3. Upserts the realised ``ServiceSdwan`` and per-site ``ServiceSdwanSite``.
4. Materialises one edge device per site, assigns a LAN address, and
   joins the device to the vendor-specific edge group so the artifact
   pipeline picks it up.
5. Sets the intent ``status`` to ``active`` on success, or ``failed``
   (with ``failure_message``) on hard error.

Idempotent at every step.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from infrahub_sdk.generator import InfrahubGenerator

from .common import find_or_create_device

LOG = logging.getLogger(__name__)

# Vendor → (platform name, device-type name, edge group name)
_VENDOR_TABLE: dict[str, tuple[str, str, str]] = {
    "viptela": ("cisco_viptela", "cEdge-1000", "sdwan_viptela_edges"),
    "versa": ("versa_flexvnf", "FlexVNF-200", "sdwan_versa_edges"),
}


class SdwanGenerator(InfrahubGenerator):
    """Materialise a realised SD-WAN service from a ServiceSdwanIntent."""

    data: dict[str, Any]

    async def generate(self, data: dict[str, Any] | None = None) -> None:
        """Generate edges + LAN IPs for every site of a single SD-WAN intent."""
        payload = data or self.data
        intent_edges = payload.get("ServiceSdwanIntent", {}).get("edges", [])
        if not intent_edges:
            LOG.warning("No ServiceSdwanIntent matched; nothing to generate")
            return
        intent = intent_edges[0]["node"]
        intent_obj = await self.client.get(
            kind="ServiceSdwanIntent",
            id=intent["id"],
            branch=self.branch,
        )

        try:
            await self._set_status(intent_obj, "in_delivery", failure=None)

            vendor = intent["vendor"]["value"]
            if vendor not in _VENDOR_TABLE:
                raise RuntimeError(f"Unknown SD-WAN vendor {vendor!r}")
            platform, device_type, edge_group_name = _VENDOR_TABLE[vendor]

            service = await self._ensure_realised_service(intent, intent_obj)

            edge_group: Any = await self.client.get(
                kind="CoreStandardGroup",
                name__value=edge_group_name,
                branch=self.branch,
            )
            await edge_group.members.fetch()
            existing_member_ids = {p.id for p in edge_group.members.peers}
            edges_to_add: list[str] = []

            for site_edge in intent["sites"]["edges"]:
                edge = await self._materialise_site(
                    site_edge["node"],
                    service=service,
                    svc_name=service.name.value,
                    platform=platform,
                    device_type=device_type,
                )
                if edge.id not in existing_member_ids:
                    edges_to_add.append(edge.id)
                    existing_member_ids.add(edge.id)

            for edge_id in edges_to_add:
                edge_group.members.add(edge_id)
            if edges_to_add:
                await edge_group.save(allow_upsert=True)

            service.status.value = "active"
            await service.save(allow_upsert=True)

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
        intent_obj.status.value = status
        intent_obj.failure_message.value = failure or ""
        await intent_obj.save(allow_upsert=True)

    async def _ensure_realised_service(
        self,
        intent: dict[str, Any],
        intent_obj: Any,
    ) -> Any:
        """Upsert the realised ServiceSdwan for this intent."""
        existing = intent.get("realised_service") or {}
        if existing.get("node"):
            return await self.client.get(
                kind="ServiceSdwan",
                id=existing["node"]["id"],
                branch=self.branch,
            )

        band = intent["band"]["value"]
        pool_name = f"sdwan_id_pool_{band}"
        pool: Any = await self.client.get(
            kind="CoreNumberPool",
            name__value=pool_name,
            branch=self.branch,
        )

        service = await self.client.create(
            kind="ServiceSdwan",
            branch=self.branch,
            name=intent["name"]["value"],
            description=intent["description"].get("value") or "",
            vendor=intent["vendor"]["value"],
            topology=intent["topology"]["value"],
            status="provisioning",
            tenant={"id": intent["tenant"]["node"]["id"]},
            service_id={"from_pool": {"id": pool.id}},
            source_intent={"id": intent["id"]},
        )
        await service.save(allow_upsert=True)

        group: Any = await self.client.get(
            kind="CoreStandardGroup",
            name__value="sdwans",
            branch=self.branch,
        )
        await group.members.fetch()
        if service.id not in {p.id for p in group.members.peers}:
            group.members.add(service.id)
            await group.save(allow_upsert=True)

        intent_obj.realised_service = service
        await intent_obj.save(allow_upsert=True)
        return service

    async def _materialise_site(
        self,
        intent_site: dict[str, Any],
        service: Any,
        svc_name: str,
        platform: str,
        device_type: str,
    ) -> Any:
        """Create realised site + edge + LAN IP for one intent site.

        Returns the edge DcimDevice so the caller can manage group membership.
        """
        site_obj = await self._ensure_realised_site(intent_site, service)
        location_name = intent_site["location"]["node"]["shortname"]["value"]

        await site_obj.sdwan_edge.fetch()
        if site_obj.sdwan_edge.peer is not None:
            edge = site_obj.sdwan_edge.peer
        else:
            edge_name = f"{svc_name}-{intent_site['name']['value']}-edge"
            edge = await find_or_create_device(
                self.client,
                name=edge_name,
                platform_name=platform,
                device_type_name=device_type,
                location_hfid=location_name,
                role="cpe",
                branch=self.branch,
            )
            site_obj.sdwan_edge = edge

        await site_obj.lan_address.fetch()
        if site_obj.lan_address.peer is None:
            lan_subnet_str = intent_site["lan_subnet"]["node"]["prefix"]["value"]
            net = ipaddress.IPv4Network(lan_subnet_str)
            lan_ip = await self.client.create(
                kind="IpamIPAddress",
                branch=self.branch,
                address=f"{net.network_address + 1}/{net.prefixlen}",
            )
            await lan_ip.save(allow_upsert=True)
            site_obj.lan_address = lan_ip

        site_obj.status.value = "active"
        await site_obj.save(allow_upsert=True)
        return edge

    async def _ensure_realised_site(
        self,
        intent_site: dict[str, Any],
        service: Any,
    ) -> Any:
        """Upsert the realised ServiceSdwanSite for one intent site."""
        existing = await self.client.filters(
            kind="ServiceSdwanSite",
            sdwan__ids=[service.id],
            name__value=intent_site["name"]["value"],
            branch=self.branch,
        )
        if existing:
            return existing[0]

        site_obj = await self.client.create(
            kind="ServiceSdwanSite",
            branch=self.branch,
            name=intent_site["name"]["value"],
            role=intent_site["role"]["value"],
            status="provisioning",
            sdwan={"id": service.id},
            location={"id": intent_site["location"]["node"]["id"]},
            lan_subnet={"id": intent_site["lan_subnet"]["node"]["id"]},
        )
        await site_obj.save(allow_upsert=True)
        return site_obj
