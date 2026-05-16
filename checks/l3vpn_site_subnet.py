"""Check that no two sites of an L3VPN have overlapping customer subnets."""

from __future__ import annotations

import ipaddress
from typing import Any

from infrahub_sdk.checks import InfrahubCheck


class L3VpnSiteSubnetCheck(InfrahubCheck):
    """Within an L3VPN, all customer subnets must be disjoint."""

    query = "l3vpn_site_subnet"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Log errors for any intra-VPN subnet overlap.

        Args:
            data: Result of the ``l3vpn_site_subnet`` GraphQL query.
        """
        for vpn_edge in data.get("ServiceL3Vpn", {}).get("edges", []):
            vpn = vpn_edge["node"]
            subnets: list[tuple[str, ipaddress.IPv4Network]] = []
            for site_edge in vpn["sites"]["edges"]:
                site = site_edge["node"]
                # Unset relationship returns ``{"node": None}`` (truthy dict).
                subnet_node = (site.get("customer_subnet") or {}).get("node")
                if not subnet_node:
                    continue
                prefix_str = subnet_node["prefix"]["value"]
                subnets.append((site["name"]["value"], ipaddress.IPv4Network(prefix_str)))

            for i, (name_a, net_a) in enumerate(subnets):
                for name_b, net_b in subnets[i + 1 :]:
                    if net_a.overlaps(net_b):
                        self.log_error(
                            message=(
                                f"L3VPN {vpn['name']['value']}: "
                                f"{name_a} subnet {net_a} overlaps {name_b} subnet {net_b}"
                            ),
                        )
