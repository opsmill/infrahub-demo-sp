"""Render-and-assert test for the Nokia SR Linux PE template (clab substitute)."""

from __future__ import annotations

import pytest

from transforms.pe_nokia_srlinux import PeNokiaSrLinux

from .fixtures import pe_fixture

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
async def test_no_l3vpn_ipv4_unicast_afi_safi() -> None:
    """SR Linux 23.10's afi-safi enum is {ipv4-unicast, ipv6-unicast, evpn}.

    Emitting `afi-safi l3vpn-ipv4-unicast` makes clab's srl postdeploy fail
    with: Invalid value 'l3vpn-ipv4-unicast': Must be
    ipv4-unicast|ipv6-unicast|evpn
    """
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    assert "l3vpn-ipv4-unicast" not in rendered


@pytest.mark.asyncio
async def test_template_is_lab_minimum_no_bgp_no_l3vpn() -> None:
    """The srlinux template is intentionally a 'lab minimum': hostname +
    interfaces + ISIS underlay only.

    BGP, per-VPN ip-vrf network-instances, bgp-vpn, and PE-CE eBGP groups
    are all skipped — every one of them tripped a different SR Linux 23.10
    parser error during the iteration cycle. Production SR OS template
    (pe_nokia_sros.j2) renders the real BGP/L3VPN config; this one just
    keeps the lab node up so the backbone topology boots.
    """
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    forbidden = [
        "protocols bgp",  # no iBGP-mesh
        "protocols bgp-vpn",  # no L3VPN signalling
        "type ip-vrf",  # no per-VPN network-instances
        "vxlan-interface",
        "ldp",  # already dropped, but lock it in
    ]
    for needle in forbidden:
        assert needle not in rendered, (
            f"{needle!r} is back in the srlinux lab template — this template "
            f"is intentionally minimum (see docstring)."
        )


@pytest.mark.asyncio
async def test_no_ldp_protocol_block() -> None:
    """SR Linux 23.10's protocols enum doesn't include `ldp`.

    Emitting `set / network-instance default protocols ldp …` makes clab's
    srl postdeploy fail with:
        Unknown token 'ldp'. Options are
        [..., 'bgp', 'bgp-evpn', 'bgp-vpn', 'isis', 'linux', 'ospf', '|']
    SR Linux uses SR-MPLS for label distribution, not LDP.
    """
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    assert "protocols ldp" not in rendered


@pytest.mark.asyncio
async def test_isis_level_capability_uses_srl_enum() -> None:
    """SR Linux's level-capability enum is L1 / L2 / L1L2 — not LEVELN.

    Previously the template did `replace('-', '') | upper`, producing
    `LEVEL2` from `level-2`, and clab's srl postdeploy rejected it with:
        Wrong value for 'value': Got 'LEVEL2' expected L1|L1L2|L2
    """
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    assert "level-capability L2" in rendered
    assert "LEVEL2" not in rendered


@pytest.mark.asyncio
async def test_ends_with_commit_save() -> None:
    """Final line commits the candidate session — without this nothing persists."""
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    assert rendered.rstrip().endswith("commit save")
