"""Render-and-assert tests for the Arista EOS CE template."""

from __future__ import annotations

import copy

import pytest

from transforms.ce_arista_eos import CeAristaEos

from .fixtures import ce_fixture


async def _render(data: dict) -> str:
    return await CeAristaEos.__new__(CeAristaEos).transform(data)


@pytest.mark.asyncio
async def test_renders_hostname_and_interfaces() -> None:
    """Hostname plus every addressed interface, including the LAN and loopback."""
    cfg = await _render(ce_fixture())
    assert "hostname ce-trading-lon" in cfg
    assert "interface Loopback0" in cfg
    assert "ip address 10.0.1.1/32" in cfg
    assert "interface Ethernet1" in cfg
    assert "ip address 10.100.0.2/30" in cfg
    assert "interface Ethernet2" in cfg
    assert "ip address 10.200.10.1/24" in cfg


@pytest.mark.asyncio
async def test_peers_with_the_pe_from_the_customer_as() -> None:
    """The CE runs BGP in its pool-allocated customer AS and peers with the PE.

    Local AS is the customer's; the neighbor is the PE-side /30 address in the
    backbone AS. Getting these backwards is the classic PE-CE misconfiguration.
    """
    cfg = await _render(ce_fixture(asn=65100))
    assert "router bgp 65100" in cfg
    assert "neighbor 10.100.0.1 remote-as 65000" in cfg
    assert "neighbor 10.100.0.1 activate" in cfg
    assert "router-id 10.0.1.1" in cfg


@pytest.mark.asyncio
async def test_advertises_the_customer_lan_and_loopback() -> None:
    """The CE originates its LAN and loopback, and nothing else."""
    cfg = await _render(ce_fixture())
    assert "network 10.200.10.0/24" in cfg
    assert "network 10.0.1.1/32" in cfg
    # `network` statements only — the CE must not leak the PE-CE transit /30
    # into the VPN via a blanket redistribute.
    assert "redistribute connected" not in cfg


@pytest.mark.asyncio
async def test_is_not_vpn_aware() -> None:
    """A CE is a plain router: no MPLS, no VRFs, no LDP.

    Those belong on the PE side of the handoff; rendering them here would be
    wrong on real hardware and would not parse on a cEOS CE.
    """
    cfg = await _render(ce_fixture())
    for token in ("mpls ip", "vrf instance", "mpls ldp", "vpn-ipv4"):
        assert token not in cfg, f"CE config must not contain '{token}'"


@pytest.mark.asyncio
async def test_without_a_session_renders_no_bgp_block() -> None:
    """Before the L3VPN generator runs there is no session, so no router bgp.

    Rendering an empty `router bgp` with no local AS would be invalid config.
    """
    cfg = await _render(ce_fixture(sessions=False))
    assert "router bgp" not in cfg
    assert "hostname ce-trading-lon" in cfg


@pytest.mark.asyncio
async def test_interface_without_an_address_is_skipped() -> None:
    """An unaddressed physical port renders no interface block.

    Ethernet1 has no IP until the generator allocates the PE-CE /30; emitting a
    bare `interface Ethernet1` would be noise in the artifact diff.
    """
    data = copy.deepcopy(ce_fixture())
    ifaces = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    next(e for e in ifaces if e["node"]["name"]["value"] == "Ethernet1")["node"]["ip_addresses"] = {
        "edges": []
    }
    cfg = await _render(data)
    assert "interface Ethernet1" not in cfg
    assert "interface Ethernet2" in cfg
