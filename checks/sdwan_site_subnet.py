"""Check that no two sites of an SD-WAN service have overlapping LAN subnets."""

from __future__ import annotations

import ipaddress
from typing import Any

from infrahub_sdk.checks import InfrahubCheck


class SdwanSiteSubnetCheck(InfrahubCheck):
    """Within a ServiceSdwan, all site LAN subnets must be disjoint."""

    query = "sdwan_site_subnet"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Log errors for any intra-service LAN subnet overlap.

        Args:
            data: Result of the ``sdwan_site_subnet`` GraphQL query.
        """
        for svc_edge in data.get("ServiceSdwan", {}).get("edges", []):
            svc = svc_edge["node"]
            subnets: list[tuple[str, ipaddress.IPv4Network]] = []
            for site_edge in svc["sites"]["edges"]:
                site = site_edge["node"]
                subnet_node = (site.get("lan_subnet") or {}).get("node")
                if not subnet_node:
                    continue
                prefix = subnet_node["prefix"]["value"]
                subnets.append((site["name"]["value"], ipaddress.IPv4Network(prefix)))

            for i, (name_a, net_a) in enumerate(subnets):
                for name_b, net_b in subnets[i + 1 :]:
                    if net_a.overlaps(net_b):
                        self.log_error(
                            message=(
                                f"SD-WAN {svc['name']['value']}: "
                                f"{name_a} subnet {net_a} overlaps {name_b} subnet {net_b}"
                            ),
                        )
