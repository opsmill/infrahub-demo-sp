"""Pure-Python validators for catalog form submissions and generator results."""

from __future__ import annotations

import ipaddress
from typing import Any


def validate_create_l3vpn_form(
    *,
    name: str,
    tenant: str,
    sites: list[dict[str, Any]],
) -> list[str]:
    """Return a list of human-readable error messages (empty = valid).

    Args:
        name: L3VPN name.
        tenant: Tenant org name.
        sites: List of site dicts as produced by the Create form.

    Returns:
        A list of error message strings; empty list means the form is valid.
    """
    errors: list[str] = []
    if not name.strip():
        errors.append("Name is required.")
    if not tenant.strip():
        errors.append("Tenant is required.")
    if len(sites) < 2:
        errors.append("L3VPN must have at least 2 sites.")

    pes = [s["pe"] for s in sites]
    if len(pes) != len(set(pes)):
        errors.append("PE reused across multiple sites in this VPN.")

    for site in sites:
        proto = site.get("routing_protocol")
        # bgp_peer_asn is optional for eBGP: left blank, the generator allocates
        # the VPN's customer AS from customer_asn_pool. Only a value that is
        # present but out of range is an error.
        asn = site.get("bgp_peer_asn")
        if proto == "ebgp" and asn is not None:
            # Coerce defensively: this function's contract is to RETURN errors,
            # so a non-numeric value (empty string from a JSON payload, a typo
            # from a non-Streamlit caller) must be reported, not raised.
            try:
                asn_value = int(asn)
            except (TypeError, ValueError):
                errors.append(f"Site {site['name']}: bgp_peer_asn must be a number (got {asn!r}).")
            else:
                if not 1 <= asn_value <= 4294967295:
                    errors.append(
                        f"Site {site['name']}: bgp_peer_asn must be between 1 and 4294967295."
                    )
        if proto == "static" and not site.get("static_routes"):
            errors.append(f"Site {site['name']}: static_routes required for static.")

    nets: list[tuple[str, ipaddress.IPv4Network]] = []
    for site in sites:
        raw = site.get("customer_subnet", "")
        try:
            net = ipaddress.IPv4Network(raw, strict=True)
        except ValueError as strict_err:
            try:
                net = ipaddress.IPv4Network(raw, strict=False)
            except ValueError:
                errors.append(
                    f"Site {site.get('name', '?')}: customer_subnet '{raw}' is not a valid "
                    f"IPv4 CIDR ({strict_err}).",
                )
                continue
            errors.append(
                f"Site {site.get('name', '?')}: customer_subnet '{raw}' has host bits set; "
                f"use the network address (e.g. {net.with_prefixlen}).",
            )
            continue
        nets.append((site["name"], net))

    for i, (n1, net1) in enumerate(nets):
        for n2, net2 in nets[i + 1 :]:
            if net1.overlaps(net2):
                errors.append(f"Subnets overlap: {n1} ({net1}) and {n2} ({net2}).")

    return errors


def validate_create_sdwan_form(
    *,
    name: str,
    tenant: str,
    vendor: str,
    topology: str,
    sites: list[dict[str, Any]],
) -> list[str]:
    """Return a list of human-readable form errors (empty on success).

    Args:
        name: Service name.
        tenant: Tenant HFID.
        vendor: ``viptela`` or ``versa``.
        topology: ``hub-spoke`` or ``full-mesh``.
        sites: List of dicts with ``name``, ``role``, ``location``, ``lan_subnet``.

    Returns:
        Error strings ready to show in the Streamlit UI.
    """
    errors: list[str] = []

    if not name.strip():
        errors.append("Name is required.")
    if not tenant:
        errors.append("Tenant is required.")
    if vendor not in {"viptela", "versa"}:
        errors.append(f"Vendor must be 'viptela' or 'versa' (got {vendor!r}).")
    if topology not in {"hub-spoke", "full-mesh"}:
        errors.append(f"Topology must be 'hub-spoke' or 'full-mesh' (got {topology!r}).")
    if len(sites) < 2:
        errors.append("An SD-WAN service needs at least two sites.")

    if topology == "hub-spoke":
        hubs = [s for s in sites if s.get("role") == "hub"]
        if len(hubs) != 1:
            errors.append("hub-spoke topology must have exactly one site with role 'hub'.")

    site_names = [s.get("name", "") for s in sites]
    if len(set(n for n in site_names if n)) != len([n for n in site_names if n]):
        errors.append("Site names must be unique within the service.")

    locations = [s.get("location", "") for s in sites]
    if len(set(loc for loc in locations if loc)) != len([loc for loc in locations if loc]):
        errors.append("Each site must use a unique location.")

    parsed: list[tuple[str, ipaddress.IPv4Network]] = []
    for site in sites:
        cidr = site.get("lan_subnet", "")
        if not cidr:
            continue
        try:
            parsed.append((site.get("name", "?"), ipaddress.IPv4Network(cidr, strict=False)))
        except ValueError:
            errors.append(f"{site.get('name', '?')}: {cidr!r} is not a valid CIDR.")

    for i, (name_a, net_a) in enumerate(parsed):
        for name_b, net_b in parsed[i + 1 :]:
            if net_a.overlaps(net_b):
                errors.append(f"{name_a} subnet {net_a} overlaps {name_b} subnet {net_b}.")

    return errors


def l3vpn_is_materialised(vpn_node: dict[str, Any] | None, *, expected_sites: int) -> bool:
    """Return whether the L3VPN generator has finished with a service.

    ``ServiceL3Vpn.status`` is not the answer. The generator sets it to
    ``active`` while materialising the VRF — before it has touched a single
    site — so a gate on status alone passes with the PE-CE addressing and the
    eBGP sessions still missing. Everything downstream of the wait then runs on
    a half-built branch: the artifacts render without the new VRF, and the
    proposed change opens showing no config diff, which is the exact failure the
    wait exists to prevent.

    What this looks at instead is the per-site output, which the generator writes
    last: every site must carry the PE port and the PE-side address, an eBGP site
    must also carry the CE-side address, and a VPN with any eBGP site that
    declined to name its own ``bgp_peer_asn`` must have had a ``customer_asn``
    allocated from the pool.

    ``expected_sites`` is checked too, because the generator can legitimately run
    against an incomplete service: creating each site emits its own event, so an
    early run sees only the sites that existed when it started, completes, and
    leaves every field this function inspects populated for those sites alone.

    Args:
        vpn_node: The ServiceL3Vpn node from a GraphQL query, or ``None`` when
            the query matched nothing.
        expected_sites: Number of sites the request asked for.

    Returns:
        True when the generator has produced everything the artifacts need.
    """
    if not vpn_node or (vpn_node.get("status") or {}).get("value") != "active":
        return False

    site_edges = (vpn_node.get("sites") or {}).get("edges") or []
    if len(site_edges) != expected_sites:
        return False

    needs_pool_asn = False
    for edge in site_edges:
        site = edge["node"]
        if not (site.get("pe_interface") or {}).get("node"):
            return False
        if not (site.get("pe_address") or {}).get("node"):
            return False
        if (site.get("routing_protocol") or {}).get("value") != "ebgp":
            continue
        if not (site.get("ce_address") or {}).get("node"):
            return False
        if (site.get("bgp_peer_asn") or {}).get("value") is None:
            needs_pool_asn = True

    return not needs_pool_asn or bool((vpn_node.get("customer_asn") or {}).get("node"))
