"""Check that no two L3VPNs share a Route Distinguisher."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck


class L3VpnOverlapCheck(InfrahubCheck):
    """No two ServiceL3Vpn rows may share an RD."""

    query = "l3vpn_overlap"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Log errors for any duplicate Route Distinguisher across L3VPNs.

        Args:
            data: Result of the ``l3vpn_overlap`` GraphQL query.
        """
        rd_to_vpns: dict[str, list[str]] = defaultdict(list)
        for edge in data.get("ServiceL3Vpn", {}).get("edges", []):
            node = edge["node"]
            # Unset relationship returns ``{"node": None}`` (truthy dict).
            # Skip VPNs whose generator hasn't materialized a VRF yet.
            vrf_rel = node.get("vrf") or {}
            vrf_node = vrf_rel.get("node")
            if not vrf_node:
                continue
            vrf_rd = vrf_node.get("vrf_rd")
            if not vrf_rd or vrf_rd.get("value") is None:
                continue
            rd = vrf_rd["value"]
            rd_to_vpns[rd].append(node["name"]["value"])

        for rd, names in rd_to_vpns.items():
            if len(names) > 1:
                self.log_error(
                    message=f"duplicate RD {rd}: used by {', '.join(names)}",
                )
