"""SD-WAN generator.

Materialises one edge device per ``ServiceSdwanSite``, allocates a LAN
address for it from the customer's LAN subnet, and adds the device to
the vendor-specific edge group so the artifact pipeline targets it.
Idempotent.
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
    """Generator that materialises everything downstream of a ServiceSdwan row."""

    data: dict[str, Any]

    async def generate(self, data: dict[str, Any] | None = None) -> None:
        """Generate edges + LAN IPs for every site of a single SD-WAN service."""
        payload = data or self.data
        svc_edges = payload.get("ServiceSdwan", {}).get("edges", [])
        if not svc_edges:
            LOG.warning("No ServiceSdwan matched; nothing to generate")
            return
        svc = svc_edges[0]["node"]
        vendor = svc["vendor"]["value"]
        if vendor not in _VENDOR_TABLE:
            raise RuntimeError(f"Unknown SD-WAN vendor {vendor!r}")
        platform, device_type, edge_group_name = _VENDOR_TABLE[vendor]

        group: Any = await self.client.get(
            kind="CoreStandardGroup",
            name__value=edge_group_name,
            branch=self.branch,
        )
        await group.members.fetch()
        existing_member_ids = {p.id for p in group.members.peers}

        edges_to_add: list[str] = []
        for site_edge in svc["sites"]["edges"]:
            edge = await self._materialise_site(
                site_edge["node"],
                svc_name=svc["name"]["value"],
                platform=platform,
                device_type=device_type,
            )
            if edge.id not in existing_member_ids:
                edges_to_add.append(edge.id)
                existing_member_ids.add(edge.id)

        if edges_to_add:
            group.members.add(edges_to_add)
            await group.save(allow_upsert=True)

        svc_obj = await self.client.get(kind="ServiceSdwan", id=svc["id"], branch=self.branch)
        svc_obj.status.value = "active"  # type: ignore[union-attr]
        await svc_obj.save(allow_upsert=True)

    async def _materialise_site(
        self,
        site: dict[str, Any],
        svc_name: str,
        platform: str,
        device_type: str,
    ) -> Any:
        """Create edge + LAN IP for one ServiceSdwanSite if not yet materialised.

        Returns the edge DcimDevice node so the caller can manage group membership.
        """
        site_obj = await self.client.get(kind="ServiceSdwanSite", id=site["id"], branch=self.branch)
        location_name = site["location"]["node"]["shortname"]["value"]

        has_edge = site.get("sdwan_edge") and site["sdwan_edge"].get("node")
        if has_edge:
            edge = await self.client.get(
                kind="DcimDevice",
                id=site["sdwan_edge"]["node"]["id"],
                branch=self.branch,
            )
        else:
            edge_name = f"{svc_name}-{site['name']['value']}-edge"
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

        has_lan = site.get("lan_address") and site["lan_address"].get("node")
        if not has_lan:
            lan_subnet_str = site["lan_subnet"]["node"]["prefix"]["value"]
            net = ipaddress.IPv4Network(lan_subnet_str)
            lan_ip = await self.client.create(
                kind="IpamIPAddress",
                branch=self.branch,
                address=f"{net.network_address + 1}/{net.prefixlen}",
            )
            await lan_ip.save(allow_upsert=True)
            site_obj.lan_address = lan_ip

        site_obj.status.value = "active"  # type: ignore[union-attr]
        await site_obj.save(allow_upsert=True)
        return edge
