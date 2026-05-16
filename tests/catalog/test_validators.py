"""Unit tests for catalog form validators."""

from __future__ import annotations

from service_catalog.utils.validators import (
    validate_create_l3vpn_form,
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
