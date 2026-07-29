"""Render-and-assert test for the Arista EOS PE template."""

from __future__ import annotations

import pytest

from transforms.pe_arista_eos import PeAristaEos

from .fixtures import pe_fixture_with_site

FIXTURE = {
    "DcimDevice": {
        "edges": [
            {
                "node": {
                    "id": "d1",
                    "name": {"value": "pe-lon-arista"},
                    "platform": {"node": {"name": {"value": "arista_eos"}}},
                    "asn": {"node": {"asn": {"value": 65000}}},
                    "interfaces": {
                        "edges": [
                            {
                                "node": {
                                    "__typename": "InterfaceVirtual",
                                    "id": "lo",
                                    "name": {"value": "Loopback0"},
                                    "description": {"value": ""},
                                    "status": {"value": "active"},
                                    "role": {"value": "management"},
                                    "mtu": {"value": 1500},
                                    "ip_addresses": {
                                        "edges": [
                                            {
                                                "node": {
                                                    "address": {"value": "10.0.0.1/32"},
                                                    "vrf": None,
                                                }
                                            }
                                        ]
                                    },
                                }
                            },
                            {
                                "node": {
                                    "__typename": "InterfacePhysical",
                                    "id": "e1",
                                    "name": {"value": "Ethernet1"},
                                    "description": {"value": "To pe-fra-cisco"},
                                    "status": {"value": "active"},
                                    "role": {"value": "core"},
                                    "mtu": {"value": 9000},
                                    "ip_addresses": {
                                        "edges": [
                                            {
                                                "node": {
                                                    "address": {"value": "10.1.0.0/31"},
                                                    "vrf": None,
                                                }
                                            }
                                        ]
                                    },
                                }
                            },
                        ]
                    },
                }
            }
        ]
    },
    "MplsIsisProcess": {
        "edges": [
            {
                "node": {
                    "area_id": {"value": "49.0001"},
                    "level": {"value": "level-2"},
                    "net_id": {"value": "49.0001.0100.0000.0001.00"},
                    "interfaces": {"edges": []},
                }
            }
        ]
    },
    "MplsLdpProcess": {
        "edges": [
            {
                "node": {
                    "router_id": {"value": "10.0.0.1"},
                    "transport_address": None,
                    "interfaces": {"edges": []},
                }
            }
        ]
    },
    "MplsBgpProcess": {
        "edges": [
            {
                "node": {
                    "router_id": {"value": "10.0.0.1"},
                    "address_families": {"value": ["vpnv4", "vpnv6"]},
                }
            }
        ]
    },
    # Queried by device from RoutingBGPSession, not via MplsBgpProcess.sessions.
    "RoutingBGPSession": {
        "edges": [
            {
                "node": {
                    "description": {"value": "lon-arista to fra-cisco"},
                    "session_type": {"value": "INTERNAL"},
                    "local_ip": {"node": {"address": {"value": "10.0.0.1/32"}}},
                    "remote_ip": {"node": {"address": {"value": "10.0.0.2/32"}}},
                    "local_as": {"node": {"asn": {"value": 65000}}},
                    "remote_as": {"node": {"asn": {"value": 65000}}},
                }
            }
        ]
    },
    "ServiceL3VpnSite": {"edges": []},
}


@pytest.mark.asyncio
async def test_renders_hostname_and_loopback() -> None:
    """Template renders hostname and Loopback0 IP."""
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    assert "hostname pe-lon-arista" in rendered
    assert "interface Loopback0" in rendered
    assert "ip address 10.0.0.1/32" in rendered


@pytest.mark.asyncio
async def test_renders_isis_net_id() -> None:
    """Template renders ISIS NET identifier."""
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    assert "router isis 1" in rendered
    assert "net 49.0001.0100.0000.0001.00" in rendered


@pytest.mark.asyncio
async def test_renders_ibgp_neighbor_and_address_families() -> None:
    """Template renders iBGP neighbor and VPNv4/VPNv6 activate."""
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    assert "router bgp 65000" in rendered
    assert "neighbor 10.0.0.2 peer group RR-MESH" in rendered
    assert "address-family vpn-ipv4" in rendered
    assert "address-family vpn-ipv6" in rendered


@pytest.mark.asyncio
async def test_renders_l3vpn_vrf_block_when_site_present() -> None:
    """Template renders vrf instance and rd when a site is attached."""
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(
        pe_fixture_with_site("pe-lon-arista", "10.0.0.1/32", "49.0001.0100.0000.0001.00")
    )
    assert "vrf instance acme-prod" in rendered
    assert "rd 65000:100" in rendered


@pytest.mark.asyncio
async def test_renders_admin_user_so_lab_ssh_works() -> None:
    """Template emits an admin user so `invoke lab.push-arista` can SSH in."""
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    assert "username admin" in rendered
    assert "role network-admin" in rendered
    assert "secret 0 admin" in rendered


@pytest.mark.asyncio
async def test_renders_management_ssh_no_shutdown() -> None:
    """Template explicitly enables management SSH so cEOS's SSHD listens."""
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    assert "management ssh" in rendered
    # The 'no shutdown' under management ssh — assert as a pair to lock it in.
    assert "management ssh\n   no shutdown" in rendered


@pytest.mark.asyncio
async def test_renders_eapi_http_for_push_arista() -> None:
    """eAPI must serve HTTP for push_arista.py — HTTPS handshake fails on cEOS-lab."""
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    assert "management api http-commands" in rendered
    assert "protocol http" in rendered
    assert "no protocol https" in rendered


@pytest.mark.asyncio
async def test_route_target_is_under_router_bgp_not_vrf_instance() -> None:
    """Modern Arista EOS rejects route-target inside `vrf instance` —
    it belongs under `router bgp <asn> / vrf <name>` with the `vpn-ipv4`
    keyword. EOS errors otherwise with:
        Invalid input (at token 0: 'route-target')
    """
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(
        pe_fixture_with_site("pe-lon-arista", "10.0.0.1/32", "49.0001.0100.0000.0001.00")
    )
    vrf_instance_section = rendered.split("vrf instance acme-prod")[1].split("!", 1)[0]
    assert "route-target" not in vrf_instance_section, (
        "route-target must not appear under vrf instance — Arista rejects it.\n"
        f"vrf instance block was:\n{vrf_instance_section}"
    )
    assert "route-target import vpn-ipv4 65000:100" in rendered
    assert "route-target export vpn-ipv4 65000:100" in rendered


@pytest.mark.asyncio
async def test_global_mpls_enable_and_ldp_interfaces() -> None:
    """Without global `mpls ip` plus per-interface `mpls ldp interface`
    declarations, LDP adjacencies never form on cEOS — the section looks
    configured but does nothing. Locks both in.
    """
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    assert "\nmpls ip\n" in rendered
    assert "transport-address interface Loopback0" in rendered
    ldp_block = rendered.split("mpls ldp\n")[1].split("\n!", 1)[0]
    assert "interface Loopback0" in ldp_block
    assert "interface Ethernet1" in ldp_block


@pytest.mark.asyncio
async def test_ibgp_send_community_extended() -> None:
    """Route-targets won't traverse the iBGP mesh without extended-community
    propagation; without this VPNv4 routes import nowhere.
    """
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    assert "neighbor RR-MESH send-community extended" in rendered


@pytest.mark.asyncio
async def test_vrf_redistribute_connected() -> None:
    """PE-CE connected /30s must be injected into VPNv4 via the VRF AF."""
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(
        pe_fixture_with_site("pe-lon-arista", "10.0.0.1/32", "49.0001.0100.0000.0001.00")
    )
    bgp_vrf_section = rendered.split("router bgp 65000\n   vrf acme-prod\n", 1)[1].split("\n!", 1)[
        0
    ]
    assert "address-family ipv4" in bgp_vrf_section
    assert "redistribute connected" in bgp_vrf_section


@pytest.mark.asyncio
async def test_emits_rancid_arista_format_marker() -> None:
    """Batfish (and other static analyzers) pick a parser by file header.

    Without a `!RANCID-CONTENT-TYPE: arista` hint, Batfish parses the config
    against the Cisco IOS grammar — every EOS-specific construct
    (`vrf instance`, `address-family vpn-ipv4`, `neighbor X peer group`,
    LDP `transport-address`, …) is reported as unrecognized syntax.
    """
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    assert "!RANCID-CONTENT-TYPE: arista" in rendered


@pytest.mark.asyncio
async def test_pe_ce_neighbor_uses_the_site_asn_override() -> None:
    """An explicit `bgp_peer_asn` on the site wins over the VPN's customer ASN."""
    fixture = pe_fixture_with_site("pe-lon-arista", "10.0.0.1/32", "49.0001.0100.0000.0001.00")
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(fixture)
    assert "neighbor 10.100.0.2 remote-as 65501" in rendered


@pytest.mark.asyncio
async def test_pe_ce_neighbor_falls_back_to_the_pool_allocated_customer_asn() -> None:
    """With no site override, the peer AS is the VPN's pool-allocated customer ASN.

    This is the default path for the financial dataset, where customer AS
    numbers come from `customer_asn_pool` rather than being written into the
    site data by hand.
    """
    fixture = pe_fixture_with_site("pe-lon-arista", "10.0.0.1/32", "49.0001.0100.0000.0001.00")
    site = fixture["ServiceL3VpnSite"]["edges"][0]["node"]
    site["bgp_peer_asn"] = {"value": None}
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(fixture)
    assert "neighbor 10.100.0.2 remote-as 65100" in rendered


@pytest.mark.asyncio
async def test_isis_does_not_enable_an_ipv6_address_family() -> None:
    """IS-IS must be IPv4-only while the dataset carries no IPv6 addressing.

    With `address-family ipv6 unicast` configured and no IPv6 address on any
    interface, cEOS silently refuses every adjacency: IIH counters climb but
    `show isis neighbors` stays empty and each router elects itself DIS.
    Verified by bisection on a live 8-PE lab — removing that one line took the
    backbone from 0/30 adjacencies to 30/30.
    """
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    isis_block = rendered.split("router isis 1", 1)[1].split("\n!", 1)[0]
    assert "address-family ipv4 unicast" in isis_block
    assert "address-family ipv6 unicast" not in isis_block


@pytest.mark.asyncio
async def test_renders_ibgp_neighbors_from_the_session_list() -> None:
    """iBGP neighbours must come from RoutingBGPSession, keyed by device.

    They used to be read from `MplsBgpProcess.sessions`, which nothing ever
    populates — so `router bgp` rendered with a peer group and no peers, and the
    backbone overlay never came up on any PE. Regression guard: the rendered
    config must contain a real neighbour.
    """
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(FIXTURE)
    assert "neighbor 10.0.0.2 peer group RR-MESH" in rendered
    assert "neighbor 10.0.0.2 remote-as 65000" in rendered
    assert "neighbor 10.0.0.2 update-source Loopback0" in rendered


@pytest.mark.asyncio
async def test_no_ibgp_sessions_renders_no_neighbors() -> None:
    """A PE with no sessions yet must not emit a dangling neighbor line."""
    import copy

    fixture = copy.deepcopy(FIXTURE)
    fixture["RoutingBGPSession"] = {"edges": []}
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(fixture)
    assert "neighbor RR-MESH peer group" in rendered  # the group itself is still defined
    assert "remote-as 65000" not in rendered
