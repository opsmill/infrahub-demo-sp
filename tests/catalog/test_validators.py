"""Unit tests for catalog form validators."""

from __future__ import annotations

from service_catalog.utils.validators import (
    l3vpn_is_materialised,
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


def test_ebgp_without_asn_defers_to_the_pool() -> None:
    """A blank ASN is valid: the generator allocates from customer_asn_pool."""
    errors = validate_create_l3vpn_form(
        name="a",
        tenant="t",
        sites=[_site(name="s1", proto="ebgp", asn=None), _site(name="s2", pe="pe-par-nokia")],
    )
    assert not any("bgp_peer_asn" in e.lower() for e in errors)


def test_ebgp_rejects_out_of_range_asn() -> None:
    errors = validate_create_l3vpn_form(
        name="a",
        tenant="t",
        sites=[
            _site(name="s1", proto="ebgp", asn=4294967296),
            _site(name="s2", pe="pe-par-nokia"),
        ],
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


def _materialised_site(
    *,
    proto: str = "ebgp",
    asn: int | None = None,
    pe_interface: bool = True,
    pe_address: bool = True,
    ce_address: bool = True,
) -> dict:
    """Build a site node shaped like the generator-progress query result.

    Args:
        proto: ``routing_protocol`` value.
        asn: Per-site ``bgp_peer_asn`` override, or ``None`` to use the pool.
        pe_interface: Whether the generator has bound the PE port yet.
        pe_address: Whether the PE-side address exists yet.
        ce_address: Whether the CE-side address exists yet.

    Returns:
        A single site edge node.
    """
    return {
        "routing_protocol": {"value": proto},
        "bgp_peer_asn": {"value": asn},
        "pe_interface": {"node": {"id": "iface-1"} if pe_interface else None},
        "pe_address": {"node": {"id": "ip-1"} if pe_address else None},
        "ce_address": {"node": {"id": "ip-2"} if ce_address else None},
    }


def _materialised_vpn(
    sites: list[dict] | None = None,
    *,
    status: str = "active",
    customer_asn: bool = True,
) -> dict:
    """Build a ServiceL3Vpn node shaped like the generator-progress query result.

    Args:
        sites: Site nodes from :func:`_materialised_site`.
        status: ``ServiceL3Vpn.status`` value.
        customer_asn: Whether a pool-allocated customer AS is linked.

    Returns:
        A ServiceL3Vpn node dict.
    """
    return {
        "status": {"value": status},
        "customer_asn": {"node": {"id": "as-1"} if customer_asn else None},
        "sites": {"edges": [{"node": s} for s in (sites or [_materialised_site()])]},
    }


def test_materialised_when_every_site_is_complete() -> None:
    vpn = _materialised_vpn([_materialised_site(), _materialised_site()])
    assert l3vpn_is_materialised(vpn, expected_sites=2)


def test_active_status_alone_is_not_materialised() -> None:
    """The regression this predicate exists for.

    The generator sets status to `active` while building the VRF, before it
    touches a site. A gate on status alone returned True here and let the caller
    render artifacts and open a proposed change against a service with no PE
    port, no PE-CE addressing and no eBGP session.
    """
    incomplete = _materialised_site(pe_interface=False, pe_address=False, ce_address=False)
    vpn = _materialised_vpn([incomplete, incomplete])
    assert not l3vpn_is_materialised(vpn, expected_sites=2)


def test_missing_pe_address_is_not_materialised() -> None:
    vpn = _materialised_vpn([_materialised_site(), _materialised_site(pe_address=False)])
    assert not l3vpn_is_materialised(vpn, expected_sites=2)


def test_partial_site_set_is_not_materialised() -> None:
    """An early run sees only the sites that existed when it started.

    Creating each site emits its own event, so a run can complete against one
    site of a two-site request and populate everything for it. Counting the
    sites is what stops that from reading as finished.
    """
    vpn = _materialised_vpn([_materialised_site()])
    assert not l3vpn_is_materialised(vpn, expected_sites=2)


def test_pool_asn_still_pending_is_not_materialised() -> None:
    vpn = _materialised_vpn([_materialised_site(), _materialised_site()], customer_asn=False)
    assert not l3vpn_is_materialised(vpn, expected_sites=2)


def test_site_asn_override_needs_no_customer_asn() -> None:
    """Every eBGP site naming its own peer AS means the pool is never consulted."""
    sites = [_materialised_site(asn=65501), _materialised_site(asn=65501)]
    assert l3vpn_is_materialised(_materialised_vpn(sites, customer_asn=False), expected_sites=2)


def test_non_ebgp_site_needs_no_ce_address() -> None:
    """A static or connected site has no PE-CE peering, so no CE-side address."""
    sites = [_materialised_site(proto="connected", ce_address=False)] * 2
    assert l3vpn_is_materialised(_materialised_vpn(sites, customer_asn=False), expected_sites=2)


def test_missing_vpn_is_not_materialised() -> None:
    """The query can return no match while the branch is still settling."""
    assert not l3vpn_is_materialised(None, expected_sites=2)
