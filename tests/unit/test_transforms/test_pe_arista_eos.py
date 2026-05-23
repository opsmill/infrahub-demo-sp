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
                    "sessions": {
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
    it belongs under `router bgp <asn> / vrf <name>`. EOS errors with:
        Invalid input (at token 0: 'route-target')
    """
    rendered = await PeAristaEos.__new__(PeAristaEos).transform(
        pe_fixture_with_site("pe-lon-arista", "10.0.0.1/32", "49.0001.0100.0000.0001.00")
    )
    # The vrf instance block must NOT contain route-target lines.
    vrf_instance_section = rendered.split("vrf instance acme-prod")[1].split("!", 1)[0]
    assert "route-target" not in vrf_instance_section, (
        "route-target must not appear under vrf instance — Arista rejects it.\n"
        f"vrf instance block was:\n{vrf_instance_section}"
    )
    # And the bgp vrf block must contain them.
    assert "route-target import 65000:100" in rendered
    assert "route-target export 65000:100" in rendered
