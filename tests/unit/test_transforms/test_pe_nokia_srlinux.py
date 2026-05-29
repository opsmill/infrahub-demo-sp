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
async def test_template_omits_unsupported_23_10_constructs() -> None:
    """Lock out every SR Linux 23.10 construct the public clab image rejects.

    The template renders iBGP (ipv4-unicast only) on top of the ISIS underlay.
    L3VPN signalling is off-limits: bgp-vpn, ip-vrf network-instances,
    l3vpn-*-unicast afi-safi, and the PE-CE eBGP group all need licensed
    ixr-class hardware that the public 23.10 image doesn't provide. LDP is
    not in 23.10's protocols enum. The `mpls` keyword is not valid under
    `network-instance default` either — the per-interface MPLS forwarding
    plane has no parser-accepting form on this image. Production SR OS
    template (pe_nokia_sros.j2) renders the real L3VPN.
    """
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    forbidden = [
        "protocols bgp-vpn",
        "type ip-vrf",
        "vxlan-interface",
        "protocols ldp",
        "l3vpn-ipv4-unicast",
        "l3vpn-ipv6-unicast",
        "network-instance default mpls",
    ]
    for needle in forbidden:
        assert needle not in rendered, (
            f"{needle!r} is back in the srlinux lab template — the public "
            f"SR Linux 23.10 image rejects it (see docstring)."
        )


@pytest.mark.asyncio
async def test_renders_ibgp_mesh_group() -> None:
    """iBGP full mesh on loopbacks. ipv4-unicast only (the only legal
    23.10 afi-safi besides ipv6 and evpn). transport.local-address pins
    sessions to the loopback — 23.10 nests local-address under transport/.
    """
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    assert "set / network-instance default protocols bgp admin-state enable" in rendered
    assert "set / network-instance default protocols bgp autonomous-system 65000" in rendered
    assert "set / network-instance default protocols bgp router-id 10.0.0.4" in rendered
    assert (
        "set / network-instance default protocols bgp afi-safi ipv4-unicast admin-state enable"
        in rendered
    )
    assert (
        "set / network-instance default protocols bgp group ibgp-mesh admin-state enable"
        in rendered
    )
    assert "set / network-instance default protocols bgp group ibgp-mesh peer-as 65000" in rendered
    assert (
        "set / network-instance default protocols bgp group ibgp-mesh transport "
        "local-address 10.0.0.4" in rendered
    )
    assert (
        "set / network-instance default protocols bgp group ibgp-mesh afi-safi "
        "ipv4-unicast admin-state enable" in rendered
    )


@pytest.mark.asyncio
async def test_renders_ibgp_neighbors_from_internal_sessions() -> None:
    """Each MplsBgpProcess session with session_type=INTERNAL produces a
    neighbor under the ibgp-mesh peer-group.
    """
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    assert (
        "set / network-instance default protocols bgp neighbor 10.0.0.1 "
        "peer-group ibgp-mesh" in rendered
    )
    assert (
        "set / network-instance default protocols bgp neighbor 10.0.0.1 admin-state enable"
        in rendered
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
async def test_core_interface_mtu_adds_l2_header_offset() -> None:
    """SR Linux `mtu` is L2 frame MTU; Arista/Cisco `mtu` is IP MTU.

    Without the +14 byte offset, SR Linux silently drops the IS-IS hellos
    cEOS pads to its IS-IS interface MTU (an 8997-byte PDU becomes a
    ~9014-byte Ethernet frame), and the IS-IS adjacency never forms.
    Fixture has core mtu=9000 → rendered template must say `mtu 9014`.
    """
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    # Fixture core iface is `Ethernet1` → srl_iface maps to `ethernet-1/1`.
    assert "set / interface ethernet-1/1 mtu 9014" in rendered


@pytest.mark.asyncio
async def test_ends_with_commit_save() -> None:
    """Final line commits the candidate session — without this nothing persists."""
    rendered = await PeNokiaSrLinux.__new__(PeNokiaSrLinux).transform(FIXTURE)
    assert rendered.rstrip().endswith("commit save")
