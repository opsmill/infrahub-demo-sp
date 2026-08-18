"""Unit tests for catalog form validators."""

from __future__ import annotations

from service_catalog.utils.validators import (
    validate_create_l3vpn_form,
    validate_create_sdwan_form,
)


def _site(
    name: str = "s",
    pe: str = "pe-lon-arista",
    subnet: str = "10.1.0.0/24",
    proto: str = "ebgp",
    asn: int | None = 65501,
    static: list | None = None,
) -> dict:
    return {
        "name": name,
        "pe": pe,
        "customer_subnet": subnet,
        "routing_protocol": proto,
        "bgp_peer_asn": asn,
        "static_routes": static,
    }


def test_minimum_two_sites_required() -> None:
    errors = validate_create_l3vpn_form(name="a", tenant="t", sites=[_site()])
    assert any("at least 2 sites" in e.lower() for e in errors)


def test_unique_pe_per_vpn() -> None:
    errors = validate_create_l3vpn_form(
        name="a",
        tenant="t",
        sites=[_site(name="s1", pe="pe-lon-arista"), _site(name="s2", pe="pe-lon-arista")],
    )
    assert any("PE reused" in e or "pe reused" in e.lower() for e in errors)


def test_ebgp_requires_asn() -> None:
    errors = validate_create_l3vpn_form(
        name="a",
        tenant="t",
        sites=[_site(name="s1", proto="ebgp", asn=None), _site(name="s2", pe="pe-par-nokia")],
    )
    assert any("bgp_peer_asn" in e.lower() for e in errors)


def test_static_requires_routes() -> None:
    errors = validate_create_l3vpn_form(
        name="a",
        tenant="t",
        sites=[
            _site(name="s1", proto="static", asn=None, static=None),
            _site(name="s2", pe="pe-par-nokia"),
        ],
    )
    assert any("static_routes" in e.lower() for e in errors)


def test_overlapping_subnets_in_same_vpn() -> None:
    errors = validate_create_l3vpn_form(
        name="a",
        tenant="t",
        sites=[
            _site(name="s1", subnet="10.1.0.0/16"),
            _site(name="s2", pe="pe-par-nokia", subnet="10.1.5.0/24"),
        ],
    )
    assert any("overlap" in e.lower() for e in errors)


def test_customer_subnet_with_host_bits_set() -> None:
    """User-typed network with host bits set should produce a hint, not a generic 'invalid'."""
    errors = validate_create_l3vpn_form(
        name="a",
        tenant="t",
        sites=[
            _site(name="s1", subnet="10.10.10.10/24"),
            _site(name="s2", pe="pe-par-nokia"),
        ],
    )
    assert any("host bits set" in e and "10.10.10.0/24" in e for e in errors)


def test_customer_subnet_garbage_string() -> None:
    """Truly malformed CIDR returns a clear 'not a valid IPv4 CIDR' error."""
    errors = validate_create_l3vpn_form(
        name="a",
        tenant="t",
        sites=[
            _site(name="s1", subnet="not-a-cidr"),
            _site(name="s2", pe="pe-par-nokia"),
        ],
    )
    assert any("not a valid IPv4 CIDR" in e for e in errors)


def test_happy_path_returns_empty() -> None:
    errors = validate_create_l3vpn_form(
        name="acme-prod",
        tenant="acme",
        sites=[
            _site(name="lon", pe="pe-lon-arista", subnet="10.10.0.0/24"),
            _site(name="par", pe="pe-par-nokia", subnet="10.20.0.0/24"),
        ],
    )
    assert errors == []


def _ok_sdwan_sites() -> list[dict]:
    return [
        {"name": "hub", "role": "hub", "location": "lon", "lan_subnet": "10.250.10.0/24"},
        {"name": "spoke-a", "role": "spoke", "location": "fra", "lan_subnet": "10.250.20.0/24"},
    ]


def test_sdwan_minimum_two_sites_required() -> None:
    errors = validate_create_sdwan_form(
        name="x",
        tenant="t",
        vendor="viptela",
        topology="hub-spoke",
        sites=[_ok_sdwan_sites()[0]],
    )
    assert any("at least two sites" in e.lower() for e in errors)


def test_sdwan_hub_required_when_hub_spoke() -> None:
    sites = [
        {"name": "a", "role": "spoke", "location": "lon", "lan_subnet": "10.250.10.0/24"},
        {"name": "b", "role": "spoke", "location": "fra", "lan_subnet": "10.250.20.0/24"},
    ]
    errors = validate_create_sdwan_form(
        name="x",
        tenant="t",
        vendor="viptela",
        topology="hub-spoke",
        sites=sites,
    )
    assert any("hub" in e.lower() for e in errors)


def test_sdwan_unique_site_names_required() -> None:
    sites = [
        {"name": "dup", "role": "hub", "location": "lon", "lan_subnet": "10.250.10.0/24"},
        {"name": "dup", "role": "spoke", "location": "fra", "lan_subnet": "10.250.20.0/24"},
    ]
    errors = validate_create_sdwan_form(
        name="x",
        tenant="t",
        vendor="viptela",
        topology="hub-spoke",
        sites=sites,
    )
    assert any("unique" in e.lower() and "name" in e.lower() for e in errors)


def test_sdwan_unique_location_required() -> None:
    sites = [
        {"name": "hub", "role": "hub", "location": "lon", "lan_subnet": "10.250.10.0/24"},
        {"name": "spoke", "role": "spoke", "location": "lon", "lan_subnet": "10.250.20.0/24"},
    ]
    errors = validate_create_sdwan_form(
        name="x",
        tenant="t",
        vendor="viptela",
        topology="hub-spoke",
        sites=sites,
    )
    assert any("location" in e.lower() for e in errors)


def test_sdwan_overlapping_lan_subnets() -> None:
    sites = [
        {"name": "hub", "role": "hub", "location": "lon", "lan_subnet": "10.250.0.0/16"},
        {"name": "spoke", "role": "spoke", "location": "fra", "lan_subnet": "10.250.10.0/24"},
    ]
    errors = validate_create_sdwan_form(
        name="x",
        tenant="t",
        vendor="viptela",
        topology="hub-spoke",
        sites=sites,
    )
    assert any("overlap" in e.lower() for e in errors)


def test_sdwan_garbage_cidr_caught() -> None:
    sites = [
        {"name": "hub", "role": "hub", "location": "lon", "lan_subnet": "not-a-cidr"},
        {"name": "spoke", "role": "spoke", "location": "fra", "lan_subnet": "10.250.20.0/24"},
    ]
    errors = validate_create_sdwan_form(
        name="x",
        tenant="t",
        vendor="viptela",
        topology="hub-spoke",
        sites=sites,
    )
    assert any("valid" in e.lower() and "cidr" in e.lower() for e in errors)


def test_sdwan_happy_path() -> None:
    errors = validate_create_sdwan_form(
        name="x",
        tenant="t",
        vendor="viptela",
        topology="hub-spoke",
        sites=_ok_sdwan_sites(),
    )
    assert errors == []
