"""Block proposed changes whose intents would exhaust a band-scoped pool.

Each ServiceL3VpnIntent / ServiceSdwanIntent that doesn't yet have a
realised service represents one pending allocation from the band's
CoreNumberPool. This check sums pending demand per pool and compares
against remaining capacity (pool range minus already-allocated IDs).

Runs as a global check on every proposed change. Logs an error per
pool that would tip over — the merge is blocked until the user picks
a different band or expands the pool.

The realised-service /30 pool (``pe_ce_pool``) isn't checked here:
the /16 supernet holds 16,384 /30s and exhaustion in this demo is
implausible. Add a similar check later if real allocation patterns
warrant it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from infrahub_sdk.checks import InfrahubCheck


def _pool_capacity(pool_node: dict[str, Any]) -> int:
    start = int(pool_node["start_range"]["value"])
    end = int(pool_node["end_range"]["value"])
    return end - start + 1


def _allocated_in_pool(pool_node: dict[str, Any], used_ids: list[int]) -> int:
    start = int(pool_node["start_range"]["value"])
    end = int(pool_node["end_range"]["value"])
    return sum(1 for v in used_ids if start <= v <= end)


def _pending_intents(
    intent_edges: list[dict[str, Any]],
) -> Counter[str]:
    """Count intents that still need to draw an ID, grouped by band."""
    counter: Counter[str] = Counter()
    for edge in intent_edges:
        node = edge["node"]
        # Already realised — generator won't reallocate.
        if (node.get("realised_service") or {}).get("node"):
            continue
        band = (node.get("band") or {}).get("value")
        if not band:
            continue
        counter[band] += 1
    return counter


class IntentPoolCapacityCheck(InfrahubCheck):
    """Pre-merge gate: every pending intent must have a free ID in its band's pool."""

    query = "intent_pool_capacity"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Walk pending intents per band and compare to remaining capacity.

        Args:
            data: Result of the ``intent_pool_capacity`` GraphQL query.
        """
        pools_by_name: dict[str, dict[str, Any]] = {
            edge["node"]["name"]["value"]: edge["node"]
            for edge in data.get("CoreNumberPool", {}).get("edges", [])
        }

        l3vpn_used_ids = [
            int(edge["node"]["vpn_id"]["value"])
            for edge in data.get("ServiceL3Vpn", {}).get("edges", [])
            if (edge["node"].get("vpn_id") or {}).get("value") is not None
        ]
        sdwan_used_ids = [
            int(edge["node"]["service_id"]["value"])
            for edge in data.get("ServiceSdwan", {}).get("edges", [])
            if (edge["node"].get("service_id") or {}).get("value") is not None
        ]

        l3vpn_pending = _pending_intents(
            data.get("ServiceL3VpnIntent", {}).get("edges", []),
        )
        sdwan_pending = _pending_intents(
            data.get("ServiceSdwanIntent", {}).get("edges", []),
        )

        self._check_one_service(
            label="L3VPN",
            pool_prefix="vpn_id_pool_",
            pending=l3vpn_pending,
            used_ids=l3vpn_used_ids,
            pools=pools_by_name,
        )
        self._check_one_service(
            label="SD-WAN",
            pool_prefix="sdwan_id_pool_",
            pending=sdwan_pending,
            used_ids=sdwan_used_ids,
            pools=pools_by_name,
        )

    def _check_one_service(
        self,
        label: str,
        pool_prefix: str,
        pending: Counter[str],
        used_ids: list[int],
        pools: dict[str, dict[str, Any]],
    ) -> None:
        for band, demand in pending.items():
            pool_name = f"{pool_prefix}{band}"
            pool = pools.get(pool_name)
            if pool is None:
                self.log_error(
                    message=(
                        f"{label} pool {pool_name!r} not found — "
                        f"intent uses band {band!r} but no matching pool is seeded"
                    ),
                )
                continue
            capacity = _pool_capacity(pool)
            allocated = _allocated_in_pool(pool, used_ids)
            free = capacity - allocated
            if demand > free:
                self.log_error(
                    message=(
                        f"{label} pool {pool_name!r} would be exhausted: "
                        f"{demand} pending intent(s) but only {free} free of "
                        f"{capacity} ids"
                    ),
                )
