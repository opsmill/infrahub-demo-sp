"""Pure-Python validators for catalog form submissions."""

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
        if proto == "ebgp" and not site.get("bgp_peer_asn"):
            errors.append(f"Site {site['name']}: bgp_peer_asn required for eBGP.")
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
