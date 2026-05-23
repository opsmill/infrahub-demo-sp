"""Render-and-assert test for the Nokia SR Linux PE template (clab substitute)."""

from __future__ import annotations

import pytest

from transforms.pe_nokia_srlinux import PeNokiaSrLinux

from .fixtures import pe_fixture, pe_fixture_with_site

FIXTURE = pe_fixture(
    name="pe-par-nokia",
    loopback="10.0.0.4/32",
    net_id="49.0001.0100.0000.0004.00",
)


@pytest.mark.asyncio
async def test_renders_hostname_with_host_name_keyword() -> None:
    """SR Linux requires the `host-name` keyword under `system name`.

    Wrong: `set / system name <value>`
    Right: `set / system name host-name <value>`

    Without it, clab's srl postdeploy step fails with:
        Parsing error: Unknown token 'pe-par-nokia'. Options are
        ['!!', '!!!', '#', '>', '>>', 'domain-name', 'host-name', '|']
    """
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    assert "set / system name host-name pe-par-nokia" in rendered


@pytest.mark.asyncio
async def test_renders_loopback_with_ipv4_address() -> None:
    """Loopback gets admin-state enable and an ipv4 address subinterface."""
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    assert "set / interface lo0 admin-state enable" in rendered
    assert "set / interface lo0 subinterface 0 ipv4 address 10.0.0.4/32" in rendered


@pytest.mark.asyncio
async def test_renders_isis_instance_and_net_id() -> None:
    """ISIS instance is created with the NET passed in as a list literal."""
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    isis_prefix = "set / network-instance default protocols isis instance i1"
    assert f"{isis_prefix} admin-state enable" in rendered
    assert "net [ 49.0001.0100.0000.0004.00 ]" in rendered


@pytest.mark.asyncio
async def test_renders_l3vpn_ip_vrf_when_site_present() -> None:
    """A site attached to an L3VPN materialises an ip-vrf network-instance."""
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(
        pe_fixture_with_site("pe-par-nokia", "10.0.0.4/32", "49.0001.0100.0000.0004.00")
    )
    assert "set / network-instance acme-prod type ip-vrf" in rendered
    assert "route-distinguisher rd 65000:100" in rendered


@pytest.mark.asyncio
async def test_ends_with_commit_save() -> None:
    """Final line commits the candidate session — without this nothing persists."""
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    assert rendered.rstrip().endswith("commit save")
