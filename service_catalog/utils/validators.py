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
