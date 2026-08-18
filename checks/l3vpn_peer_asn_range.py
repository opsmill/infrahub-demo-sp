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
            # Say so rather than passing silently. A renamed or missing pool used
            # to make this check succeed while enforcing nothing, so the day the
            # pool moved was the day the guarantee quietly disappeared.
            self.log_error(
                message=(
                    f"Cannot evaluate peer-AS overrides: no CoreNumberPool named "
                    f"{POOL_NAME} with both bounds set was returned. If the pool was "
                    f"renamed, update POOL_NAME in checks/l3vpn_peer_asn_range.py and "
                    f"the filter in queries/validation/l3vpn_peer_asn_range.gql."
                ),
            )
            return
        start, end = bounds
        backbone = self._backbone_asns(data)

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
                where = f"L3VPN {vpn['name']['value']}: site {site['name']['value']}"
                if asn in backbone:
                    self.log_error(
                        message=(
                            f"{where} sets bgp_peer_asn {asn}, which is the backbone's "
                            f"own AS. The generator would adopt the provider AS as the "
                            f"customer side of an eBGP session — every PE's device ASN "
                            f"and the whole iBGP mesh reference that row. Use a customer "
                            f"AS, or leave bgp_peer_asn unset to take one from "
                            f"{POOL_NAME}."
                        ),
                    )
                elif start <= asn <= end:
                    self.log_error(
                        message=(
                            f"{where} sets bgp_peer_asn {asn}, which is "
                            f"inside {POOL_NAME}'s {start}-{end} range. The pool can "
                            f"issue that same number to another VPN, and "
                            f"RoutingAutonomousSystem.asn is unique. Pick a number "
                            f"outside {start}-{end}, or leave bgp_peer_asn unset to "
                            f"take one from the pool."
                        ),
                    )

    @staticmethod
    def _backbone_asns(data: dict[str, Any]) -> set[int]:
        """Return every backbone AS number in the query result.

        Args:
            data: Result of the ``l3vpn_peer_asn_range`` GraphQL query.

        Returns:
            The provider AS numbers, empty when none could be read.
        """
        found: set[int] = set()
        for edge in data.get("TopologyMplsBackbone", {}).get("edges", []):
            asn = ((edge["node"].get("asn") or {}).get("node") or {}).get("asn", {}).get("value")
            if asn is None:
                continue
            try:
                found.add(int(asn))
            except (TypeError, ValueError):
                continue
        return found

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
