"""Block edits to active intents.

Once a ServiceL3VpnIntent or ServiceSdwanIntent reaches ``status = active``
on main, its tracked fields become immutable. To change anything,
create a new intent rather than edit the active one — the realised
graph stays a faithful reproduction of the request that produced it.

``status`` and ``failure_message`` are deliberately *not* tracked: the
generator must remain free to update those during proposed-change
preview without tripping this check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

# Tracked fields per intent kind. A diff in any of these on a branch
# whose main-side counterpart is `active` triggers a log_error.
_L3VPN_TRACKED_ATTRS: tuple[str, ...] = ("description", "band", "address_family")
_SDWAN_TRACKED_ATTRS: tuple[str, ...] = ("description", "band", "vendor", "topology")

# Single source of truth for the query — read once at import time.
# Layout: checks/intent_immutability.py + queries/validation/intent_immutability.gql
_QUERY_PATH = Path(__file__).parent.parent / "queries" / "validation" / "intent_immutability.gql"
_INTENT_IMMUTABILITY_QUERY = _QUERY_PATH.read_text(encoding="utf-8")


def _normalise_intent(node: dict[str, Any], site_attrs: tuple[str, ...]) -> dict[str, Any]:
    """Return a hashable, comparable view of an intent for diffing.

    Args:
        node: GraphQL node dict from the IntentImmutability query.
        site_attrs: Site-level attrs that participate in the comparison.
    """
    tenant_rel = node.get("tenant") or {}
    tenant_node = tenant_rel.get("node") or {}
    sites = []
    for site_edge in node.get("sites", {}).get("edges", []) or []:
        site_node = site_edge["node"]
        site_view: dict[str, Any] = {"name": site_node["name"]["value"]}
        for attr in site_attrs:
            site_view[attr] = (site_node.get(attr) or {}).get("value")
        for rel in ("pe_device", "customer_subnet", "location", "lan_subnet"):
            rel_data = site_node.get(rel)
            if rel_data is not None:
                rel_node = (rel_data or {}).get("node") or {}
                # rely on a stable identity field — name or prefix
                identity = (rel_node.get("name") or {}).get("value") or (
                    rel_node.get("prefix") or {}
                ).get("value")
                site_view[rel] = identity
        sites.append(site_view)
    sites.sort(key=lambda s: s["name"])
    return {
        "tenant": (tenant_node.get("name") or {}).get("value"),
        "sites": sites,
    }


class IntentImmutabilityCheck(InfrahubCheck):
    """Reject branch-side edits to intents that are active on main."""

    query = "intent_immutability"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Compare branch-side intents against the main-side baseline.

        Args:
            data: Result of the ``intent_immutability`` GraphQL query
                evaluated on the proposed-change branch.
        """
        main_data = await self.client.execute_graphql(
            query=_INTENT_IMMUTABILITY_QUERY,
            branch_name="main",
        )

        await self._diff_kind(
            branch_intents=data.get("ServiceL3VpnIntent", {}).get("edges", []),
            main_intents=main_data.get("ServiceL3VpnIntent", {}).get("edges", []),
            tracked_attrs=_L3VPN_TRACKED_ATTRS,
            site_attrs=("routing_protocol", "bgp_peer_asn", "static_routes"),
            label="L3VPN intent",
        )
        await self._diff_kind(
            branch_intents=data.get("ServiceSdwanIntent", {}).get("edges", []),
            main_intents=main_data.get("ServiceSdwanIntent", {}).get("edges", []),
            tracked_attrs=_SDWAN_TRACKED_ATTRS,
            site_attrs=("role",),
            label="SD-WAN intent",
        )

    async def _diff_kind(
        self,
        branch_intents: list[dict[str, Any]],
        main_intents: list[dict[str, Any]],
        tracked_attrs: tuple[str, ...],
        site_attrs: tuple[str, ...],
        label: str,
    ) -> None:
        main_by_name = {
            e["node"]["name"]["value"]: e["node"]
            for e in main_intents
            if e.get("node") and e["node"].get("status", {}).get("value") == "active"
        }
        for branch_edge in branch_intents:
            branch_node = branch_edge["node"]
            name = branch_node["name"]["value"]
            main_node = main_by_name.get(name)
            if not main_node:
                continue  # not active on main — edits allowed

            diffs: list[str] = []
            for attr in tracked_attrs:
                branch_val = (branch_node.get(attr) or {}).get("value")
                main_val = (main_node.get(attr) or {}).get("value")
                if branch_val != main_val:
                    diffs.append(f"{attr}: {main_val!r} -> {branch_val!r}")

            branch_view = _normalise_intent(branch_node, site_attrs)
            main_view = _normalise_intent(main_node, site_attrs)
            if branch_view["tenant"] != main_view["tenant"]:
                diffs.append(f"tenant: {main_view['tenant']!r} -> {branch_view['tenant']!r}")
            if branch_view["sites"] != main_view["sites"]:
                diffs.append("sites: composition changed")

            if diffs:
                self.log_error(
                    message=(
                        f"{label} {name!r} is active on main; "
                        f"tracked field(s) changed: {'; '.join(diffs)}"
                    ),
                )
