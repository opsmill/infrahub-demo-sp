"""Check that no two L3VPNs share a Route Distinguisher."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck


class L3VpnOverlapCheck(InfrahubCheck):
    """No two ServiceL3Vpn rows may share an RD."""

    query = "l3vpn_overlap"

    async def validate(self, data: dict[str, Any]) -> list[str]:  # type: ignore[override]
        """Return list of error messages (empty = pass).

        Args:
            data: Result of the ``l3vpn_overlap`` GraphQL query.

        Returns:
            List of human-readable failure messages.
        """
        rd_to_vpns: dict[str, list[str]] = defaultdict(list)
        for edge in data.get("ServiceL3Vpn", {}).get("edges", []):
            node = edge["node"]
            if not node.get("vrf"):
                continue
            rd = node["vrf"]["node"]["vrf_rd"]["value"]
            rd_to_vpns[rd].append(node["name"]["value"])

        errors: list[str] = []
        for rd, names in rd_to_vpns.items():
            if len(names) > 1:
                errors.append(f"duplicate RD {rd}: used by {', '.join(names)}")
        return errors
