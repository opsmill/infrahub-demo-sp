"""Check that no two SD-WAN services share a service_id."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck


class SdwanIdOverlapCheck(InfrahubCheck):
    """No two ServiceSdwan rows may share a service_id."""

    query = "sdwan_id_overlap"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Log errors when two services collide on service_id.

        Args:
            data: Result of the ``sdwan_id_overlap`` GraphQL query.
        """
        id_to_names: dict[int, list[str]] = defaultdict(list)
        for edge in data.get("ServiceSdwan", {}).get("edges", []):
            node = edge["node"]
            sid_field = node.get("service_id") or {}
            sid = sid_field.get("value")
            if sid is None:
                continue
            id_to_names[int(sid)].append(node["name"]["value"])

        for sid, names in id_to_names.items():
            if len(names) > 1:
                self.log_error(
                    message=f"duplicate service_id {sid}: used by {', '.join(names)}",
                )
