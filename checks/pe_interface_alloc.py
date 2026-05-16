"""Check that PE interfaces are not double-claimed by L3VPN sites."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck


class PeInterfaceAllocCheck(InfrahubCheck):
    """No PE interface is bound to more than one L3VPN site."""

    query = "pe_interface_alloc"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Log errors when any (pe, interface) tuple is claimed by 2+ sites.

        Args:
            data: Result of the ``pe_interface_alloc`` GraphQL query.
        """
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for edge in data.get("ServiceL3VpnSite", {}).get("edges", []):
            node = edge["node"]
            # Unset relationships are returned as ``{"node": None}`` (truthy
            # dict). Skip sites whose generator hasn't yet allocated an
            # interface or attached the PE device.
            iface_node = (node.get("pe_interface") or {}).get("node")
            pe_node = (node.get("pe_device") or {}).get("node")
            if not iface_node or not pe_node:
                continue
            key = (pe_node["name"]["value"], iface_node["id"])
            groups[key].append(node["name"]["value"])

        for (pe, _), sites in groups.items():
            if len(sites) > 1:
                self.log_error(
                    message=f"PE {pe} interface double-claimed by sites: {', '.join(sites)}",
                )
