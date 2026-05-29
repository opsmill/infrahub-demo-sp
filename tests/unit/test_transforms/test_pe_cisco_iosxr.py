"""Render-and-assert test for the Cisco IOS-XR PE template."""

from __future__ import annotations

import pytest

from transforms.pe_cisco_iosxr import PeCiscoIosXr

from .fixtures import pe_fixture, pe_fixture_with_site

FIXTURE = pe_fixture(
    name="pe-fra-cisco",
    loopback="10.0.0.2/32",
    net_id="49.0001.0100.0000.0002.00",
)


@pytest.mark.asyncio
async def test_renders_hostname_and_loopback() -> None:
    """Template renders hostname and Loopback0 IPv4 address."""
    rendered = await PeCiscoIosXr.__new__(PeCiscoIosXr).transform(FIXTURE)
    assert "hostname pe-fra-cisco" in rendered
    assert "interface Loopback0" in rendered
    assert "10.0.0.2" in rendered


@pytest.mark.asyncio
async def test_renders_isis_net_id() -> None:
    """Template renders ISIS NET identifier with IOS-XR level-2-only keyword."""
    rendered = await PeCiscoIosXr.__new__(PeCiscoIosXr).transform(FIXTURE)
    assert "router isis 1" in rendered
    assert "net 49.0001.0100.0000.0002.00" in rendered
    assert "is-type level-2-only" in rendered


@pytest.mark.asyncio
async def test_renders_ibgp_and_vpnv4_families() -> None:
    """Template renders iBGP neighbor and vpnv4/vpnv6 unicast address families."""
    rendered = await PeCiscoIosXr.__new__(PeCiscoIosXr).transform(FIXTURE)
    assert "router bgp 65000" in rendered
    assert "address-family vpnv4 unicast" in rendered
    assert "route-policy PASS-ALL" in rendered


@pytest.mark.asyncio
async def test_renders_l3vpn_vrf_block_when_site_present() -> None:
    """Template renders vrf definition and route-target import when a site is attached."""
    rendered = await PeCiscoIosXr.__new__(PeCiscoIosXr).transform(
        pe_fixture_with_site("pe-fra-cisco", "10.0.0.2/32", "49.0001.0100.0000.0002.00")
    )
    assert "vrf acme-prod" in rendered
    assert "import route-target" in rendered


@pytest.mark.asyncio
async def test_translates_arista_iface_to_iosxr_gig_form() -> None:
    """Schema names are `Ethernet<N>` (1-indexed); IOS-XR needs
    `GigabitEthernet0/0/0/<N-1>`.

    Without translation the rendered config would have `interface Ethernet1`
    which IOS-XR rejects. Batfish parses the unconverted name as a config
    error against the IOS-XR grammar.
    """
    rendered = await PeCiscoIosXr.__new__(PeCiscoIosXr).transform(
        pe_fixture_with_site("pe-fra-cisco", "10.0.0.2/32", "49.0001.0100.0000.0002.00")
    )
    # Schema form must never appear verbatim as a real (non-Loopback) interface.
    assert "interface Ethernet1" not in rendered
    assert "interface Ethernet4" not in rendered
    # Core backbone iface (Ethernet1) → GigabitEthernet0/0/0/0.
    assert "interface GigabitEthernet0/0/0/0" in rendered
    # Per-VRF customer iface (Ethernet4) → GigabitEthernet0/0/0/3.
    assert "interface GigabitEthernet0/0/0/3" in rendered
