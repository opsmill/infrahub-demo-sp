"""Check that no hand-picked peer AS collides with the customer ASN pool.

``customer_asn_pool`` issues one customer AS per L3VPN, and a site may override
it with an explicit ``bgp_peer_asn``. Nothing in the schema stops that override
from naming a number the pool also hands out, and the two allocation paths
cannot see each other: the pool tracks only its own reservations, while an
override is resolved by ``asn__value`` lookup. An override inside the pool range
therefore either adopts another VPN's pool-issued AS or is later duplicated by
the pool, which fails on ``RoutingAutonomousSystem.asn`` being unique.

The datasets keep their seeded AS numbers outside the range by convention (see
objects/50_pools.yml); this check is what makes the convention enforceable.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

POOL_NAME = "customer_asn_pool"


class L3VpnPeerAsnRangeCheck(InfrahubCheck):
    """No site's ``bgp_peer_asn`` may fall inside the customer ASN pool range."""

    query = "l3vpn_peer_asn_range"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Log an error for every site override that lands inside the pool range.

        Args:
            data: Result of the ``l3vpn_peer_asn_range`` GraphQL query.
        """
        bounds = self._pool_bounds(data)
        if bounds is None:
            # No pool, or a pool with open bounds: there is no range to collide
            # with, so every override is safe by definition.
            return
        start, end = bounds

        for vpn_edge in data.get("ServiceL3Vpn", {}).get("edges", []):
            vpn = vpn_edge["node"]
            for site_edge in vpn.get("sites", {}).get("edges", []):
                site = site_edge["node"]
                # Unset attribute returns ``{"value": None}`` (truthy dict).
                override = (site.get("bgp_peer_asn") or {}).get("value")
                if override is None:
                    continue
                try:
                    asn = int(override)
                except (TypeError, ValueError):
                    continue
                if start <= asn <= end:
                    self.log_error(
                        message=(
                            f"L3VPN {vpn['name']['value']}: site "
                            f"{site['name']['value']} sets bgp_peer_asn {asn}, which is "
                            f"inside {POOL_NAME}'s {start}-{end} range. The pool can "
                            f"issue that same number to another VPN, and "
                            f"RoutingAutonomousSystem.asn is unique. Pick a number "
                            f"outside {start}-{end}, or leave bgp_peer_asn unset to "
                            f"take one from the pool."
                        ),
                    )

    @staticmethod
    def _pool_bounds(data: dict[str, Any]) -> tuple[int, int] | None:
        """Return the customer ASN pool's ``(start, end)``, or ``None`` if unknown.

        Args:
            data: Result of the ``l3vpn_peer_asn_range`` GraphQL query.

        Returns:
            The inclusive pool bounds, or ``None`` when the pool is absent or
            either bound is unset.
        """
        for edge in data.get("CoreNumberPool", {}).get("edges", []):
            node = edge["node"]
            if node.get("name", {}).get("value") != POOL_NAME:
                continue
            start = (node.get("start_range") or {}).get("value")
            end = (node.get("end_range") or {}).get("value")
            if start is None or end is None:
                return None
            try:
                return int(start), int(end)
            except (TypeError, ValueError):
                return None
        return None
