"""Render-and-assert tests for the Arista EOS CE template."""

from __future__ import annotations

import copy

import pytest

from transforms.ce_arista_eos import CeAristaEos

from .fixtures import ce_fixture


async def _render(data: dict) -> str:
    """Render the CE template against ``data``, bypassing transform __init__.

    ``__new__`` avoids InfrahubTransform's constructor, which wants a live
    client; ``transform`` itself only needs the query result.

    Args:
        data: A ``ce`` query-result fixture.

    Returns:
        The rendered EOS configuration.
    """
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


def _second_vpn_session(local_asn: int) -> dict:
    """Return a second CE-PE session, for a different L3VPN in ``local_asn``.

    Models one CE terminating sites of two L3VPNs — legal in the schema, and the
    reason the local AS cannot live on the device alone.

    Args:
        local_asn: The other VPN's customer AS.

    Returns:
        A RoutingBGPSession edge shaped like the ``ce`` query result.
    """
    return {
        "node": {
            "description": {"value": "L3VPN CE-PE ib-advisory-vpn ib-london"},
            "local_ip": {"node": {"address": {"value": "10.100.0.6/30"}}},
            "remote_ip": {"node": {"address": {"value": "10.100.0.5/30"}}},
            "local_as": {"node": {"asn": {"value": local_asn}}},
            "remote_as": {"node": {"asn": {"value": 65000}}},
        }
    }


@pytest.mark.asyncio
async def test_second_customer_as_is_announced_per_neighbor() -> None:
    """A CE serving two L3VPNs keeps one BGP instance and shifts AS per neighbor.

    EOS runs a single BGP instance, and DcimDevice.asn holds a single AS, so the
    second VPN's customer AS has to reach the config through its session. Without
    this the instance AS covered both peerings and the second one came up in the
    wrong AS, so it never established.
    """
    data = copy.deepcopy(ce_fixture(asn=65100))
    data["RoutingBGPSession"]["edges"].append(_second_vpn_session(65101))
    cfg = await _render(data)

    assert "router bgp 65100" in cfg
    assert cfg.count("router bgp") == 1, "EOS allows only one BGP instance"
    # The site in the instance AS needs no override; the other one does.
    assert "neighbor 10.100.0.1 local-as" not in cfg
    assert "neighbor 10.100.0.5 local-as 65101 no-prepend replace-as" in cfg


@pytest.mark.asyncio
async def test_no_local_as_override_when_every_site_shares_one_as() -> None:
    """The common case stays clean: one customer AS, no per-neighbor local-as."""
    data = copy.deepcopy(ce_fixture(asn=65100))
    data["RoutingBGPSession"]["edges"].append(_second_vpn_session(65100))
    cfg = await _render(data)
    assert "local-as" not in cfg


@pytest.mark.asyncio
async def test_bgp_instance_falls_back_to_the_session_as() -> None:
    """A CE whose `asn` was never set still renders a usable instance AS.

    The generator only claims DcimDevice.asn for the first site that needs it, so
    a CE adopted mid-flight can have sessions but no device AS. Rendering
    `router bgp None` there would be invalid config.
    """
    data = copy.deepcopy(ce_fixture(asn=65100))
    data["DcimDevice"]["edges"][0]["node"]["asn"] = {"node": None}
    cfg = await _render(data)
    assert "router bgp 65100" in cfg
    assert "router bgp None" not in cfg


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


@pytest.mark.asyncio
async def test_renders_tagged_subinterface_for_the_customer_vlan() -> None:
    """The private side is a dot1q sub-interface carrying the allocated VLAN.

    The VLAN comes from that customer's own pool, so the tag identifies the
    customer. The LAN gateway lives on the sub-interface, not the parent port.
    """
    cfg = await _render(ce_fixture(vlan=110, lan_address="10.200.10.1/24"))
    assert "interface Ethernet2.110" in cfg
    assert "encapsulation dot1q vlan 110" in cfg
    sub = cfg.split("interface Ethernet2.110", 1)[1].split("\n!", 1)[0]
    assert "ip address 10.200.10.1/24" in sub


@pytest.mark.asyncio
async def test_dot1q_parent_is_rendered_but_unaddressed() -> None:
    """The parent port must still appear, even though it carries no address.

    Rendering physical ports on "has an address" alone would drop the parent of
    a tagged handoff entirely, and the sub-interface would have nothing to
    hang off on the device.
    """
    cfg = await _render(ce_fixture(vlan=110))
    parent = cfg.split("interface Ethernet2\n", 1)[1].split("\n!", 1)[0]
    assert "no switchport" in parent
    assert "ip address" not in parent


@pytest.mark.asyncio
async def test_loopback_is_not_treated_as_a_subinterface() -> None:
    """Loopback0 is virtual too, but has no VLAN or parent — it must not be tagged."""
    cfg = await _render(ce_fixture())
    loopback = cfg.split("interface Loopback0", 1)[1].split("\n!", 1)[0]
    assert "encapsulation" not in loopback
    assert "ip address 10.0.1.1/32" in loopback
