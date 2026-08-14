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

from .common import DEFAULT_IP_NAMESPACE, find_or_create_device, touch

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

        edges_to_add: list[str] = []
        for site_edge in svc["sites"]["edges"]:
            edge = await self._materialise_site(
                site_edge["node"],
                svc_name=svc["name"]["value"],
                platform=platform,
                device_type=device_type,
            )
            edges_to_add.append(edge.id)

        if edges_to_add:
            # RelationshipAdd, not a read-modify-write of `members`. Saving the
            # group would re-send its whole member list as a replacement, and
            # the list is only ever one fetched page — every member outside that
            # page would be dropped from the group, which for the SD-WAN edge
            # groups means silently unbinding other services' edge devices from
            # their artifact definitions. Adding is idempotent server-side, so
            # re-running needs no membership diff of our own.
            await group.add_relationships(relation_to_update="members", related_nodes=edges_to_add)

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

        # Idempotency is derived from deterministic keys via ``client.filters``,
        # NOT from the generator query. The query must not return ``sdwan_edge``
        # / ``lan_address`` (the objects this generator creates): if it did, the
        # generator's internal query-group (``collect_data(update_group=True)``)
        # would track those freshly-created nodes and destabilise — its
        # ``CoreGraphQLQueryGroupUpsert`` raises ``NodeNotFound`` and the branch
        # gets wiped inside the proposed-change pipeline. Looking state up by
        # key here keeps idempotency without that coupling, the same way
        # infrahub-demo-dc's generators look existing objects up separately.
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

        net = ipaddress.IPv4Network(site["lan_subnet"]["node"]["prefix"]["value"])
        lan_addr = f"{net.network_address + 1}/{net.prefixlen}"
        # Pinned to the provider namespace. SD-WAN LAN addresses are provider
        # space, while each L3VPN's customer space now has a namespace of its own,
        # so an unscoped lookup on the address alone could return an L3VPN
        # customer's LAN gateway and rebind it as this site's lan_address.
        existing_ip = await self.client.filters(
            kind="IpamIPAddress",
            address__value=lan_addr,
            ip_namespace__name__value=DEFAULT_IP_NAMESPACE,
            branch=self.branch,
        )
        if existing_ip:
            # See generators/common.py: touch() carries the reaper rationale.
            lan_ip = await touch(existing_ip[0])
        else:
            lan_ip = await self.client.create(
                kind="IpamIPAddress", branch=self.branch, address=lan_addr
            )
            await lan_ip.save(allow_upsert=True)
        site_obj.lan_address = lan_ip

        site_obj.status.value = "active"  # type: ignore[union-attr]
        await site_obj.save(allow_upsert=True)
        return edge
