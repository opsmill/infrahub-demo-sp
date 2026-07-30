"""Shared fixture factory for PE config transform tests."""

from __future__ import annotations

import copy


def pe_fixture(name: str, loopback: str, net_id: str) -> dict:
    """Return a parametrised PE query-result fixture.

    Args:
        name: Device hostname (e.g. ``"pe-fra-cisco"``).
        loopback: Loopback0 address in CIDR notation (e.g. ``"10.0.0.2/32"``).
        net_id: ISIS NET identifier (e.g. ``"49.0001.0100.0000.0002.00"``).

    Returns:
        Dictionary matching the shape returned by the ``pe`` GraphQL query.
    """
    loopback_ip = loopback.split("/")[0]
    return {
        "DcimDevice": {
            "edges": [
                {
                    "node": {
                        "id": "d1",
                        "name": {"value": name},
                        "platform": {"node": {"name": {"value": "generic"}}},
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
                                                        "address": {"value": loopback},
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
                                        # Schema convention: abstract Ethernet<N> (1-indexed).
                                        # Per-vendor templates translate via _macros.j2
                                        # (`iosxr_iface`, `junos_iface`, `srl_iface`).
                                        "name": {"value": "Ethernet1"},
                                        "description": {"value": "To backbone peer"},
                                        "status": {"value": "active"},
                                        "role": {"value": "core"},
                                        "mtu": {"value": 9000},
                                        "ip_addresses": {
                                            "edges": [
                                                {
                                                    "node": {
                                                        "address": {"value": "10.1.0.2/31"},
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
                        # Matches the schema's Dropdown choices (level-1, level-2, level-1-2).
                        # The Cisco IOS-XR template hard-codes 'level-2-only' separately.
                        "level": {"value": "level-2"},
                        "net_id": {"value": net_id},
                        "interfaces": {"edges": []},
                    }
                }
            ]
        },
        "MplsLdpProcess": {
            "edges": [
                {
                    "node": {
                        "router_id": {"value": loopback_ip},
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
                        "router_id": {"value": loopback_ip},
                        "address_families": {"value": ["vpnv4", "vpnv6"]},
                    }
                }
            ]
        },
        # iBGP sessions are queried from RoutingBGPSession by device, not via
        # MplsBgpProcess.sessions (which nothing populates).
        "RoutingBGPSession": {
            "edges": [
                {
                    "node": {
                        "description": {"value": "iBGP to peer"},
                        "session_type": {"value": "INTERNAL"},
                        "local_ip": {"node": {"address": {"value": loopback}}},
                        "remote_ip": {"node": {"address": {"value": "10.0.0.1/32"}}},
                        "local_as": {"node": {"asn": {"value": 65000}}},
                        "remote_as": {"node": {"asn": {"value": 65000}}},
                    }
                }
            ]
        },
        "ServiceL3VpnSite": {"edges": []},
    }


def pe_fixture_with_site(name: str, loopback: str, net_id: str) -> dict:
    """Return a PE fixture with one ServiceL3VpnSite to exercise VRF code paths.

    The L3VPN, VRF, and site use fixed values that are enough to drive the
    VRF/PE-CE sections of every vendor template.

    Args:
        name: Device hostname (e.g. ``"pe-lon-arista"``).
        loopback: Loopback0 address in CIDR notation (e.g. ``"10.0.0.1/32"``).
        net_id: ISIS NET identifier (e.g. ``"49.0001.0100.0000.0001.00"``).

    Returns:
        Dictionary matching the shape returned by the ``pe`` GraphQL query,
        with one site attached to the ``acme-prod`` L3VPN.
    """
    base = pe_fixture(name, loopback, net_id)
    fixture = copy.deepcopy(base)

    l3vpn_node = {
        "name": {"value": "acme-prod"},
        "vpn_id": {"value": 100},
        "customer_asn": {"node": {"asn": {"value": 65100}}},
        "vrf": {
            "node": {
                "name": {"value": "acme-prod"},
                "vrf_rd": {"value": "65000:100"},
                "import_rt": {"node": {"name": {"value": "65000:100"}}},
                "export_rt": {"node": {"name": {"value": "65000:100"}}},
            }
        },
    }

    site_node = {
        "name": {"value": "lon"},
        "l3vpn": {"node": l3vpn_node},
        "pe_interface": {"node": {"name": {"value": "Ethernet4"}}},
        "customer_subnet": {"value": "192.168.1.0/24"},
        "pe_address": {"node": {"address": {"value": "10.100.0.1/30"}}},
        "ce_address": {"node": {"address": {"value": "10.100.0.2/30"}}},
        "routing_protocol": {"value": "ebgp"},
        "bgp_peer_asn": {"value": 65501},
        "static_routes": {"value": []},
    }

    fixture["ServiceL3VpnSite"] = {"edges": [{"node": site_node}]}
    return fixture


def ce_fixture(
    *,
    name: str = "ce-trading-lon",
    asn: int = 65100,
    loopback: str = "10.0.1.1/32",
    pe_facing_address: str = "10.100.0.2/30",
    pe_address: str = "10.100.0.1/30",
    lan_address: str = "10.200.10.1/24",
    customer_subnet: str = "10.200.10.0/24",
    vlan: int = 110,
    sessions: bool = True,
) -> dict:
    """Return a CE query-result fixture for the ``ce`` GraphQL query.

    Args:
        name: CE hostname.
        asn: The customer AS the CE peers from (allocated from customer_asn_pool).
        loopback: Loopback0 address in CIDR notation.
        pe_facing_address: CE-side address of the PE-CE /30, on Ethernet1.
        pe_address: PE-side address of the PE-CE /30 (the eBGP neighbor).
        lan_address: Customer LAN gateway address on Ethernet2.
        customer_subnet: The LAN prefix the CE advertises into the VPN.
        vlan: dot1q tag on the private-side sub-interface, from the customer pool.
        sessions: When ``False``, omit the eBGP session — models a CE whose
            L3VPN generator hasn't run yet.

    Returns:
        Dictionary matching the shape returned by the ``ce`` GraphQL query.
    """

    def _iface(
        typename: str,
        iface_name: str,
        description: str,
        address: str | None,
        *,
        dot1q: int | None = None,
        parent: str | None = None,
    ) -> dict:
        addresses = [{"node": {"address": {"value": address}}}] if address else []
        node: dict = {
            "__typename": typename,
            "id": iface_name,
            "name": {"value": iface_name},
            "description": {"value": description},
            "status": {"value": "active"},
            "role": {"value": "access"},
            "mtu": {"value": 1500},
            "ip_addresses": {"edges": addresses},
        }
        if typename == "InterfaceVirtual":
            # The `ce` query returns these for every InterfaceVirtual; the
            # loopback simply has them empty.
            node["dot1q_id"] = {"value": dot1q}
            node["parent_interface"] = (
                {"node": {"name": {"value": parent}}} if parent else {"node": None}
            )
        return {"node": node}

    session_edges = []
    if sessions:
        session_edges = [
            {
                "node": {
                    "description": {"value": f"L3VPN CE-PE trading-floor-vpn {name}"},
                    "local_ip": {"node": {"address": {"value": pe_facing_address}}},
                    "remote_ip": {"node": {"address": {"value": pe_address}}},
                    "local_as": {"node": {"asn": {"value": asn}}},
                    "remote_as": {"node": {"asn": {"value": 65000}}},
                }
            }
        ]

    return {
        "DcimDevice": {
            "edges": [
                {
                    "node": {
                        "id": "ce-id",
                        "name": {"value": name},
                        "platform": {"node": {"name": {"value": "arista_eos"}}},
                        "asn": {"node": {"asn": {"value": asn}}},
                        "interfaces": {
                            "edges": [
                                _iface("InterfaceVirtual", "Loopback0", "Router-ID", loopback),
                                _iface(
                                    "InterfacePhysical",
                                    "Ethernet1",
                                    "To pe-01",
                                    pe_facing_address,
                                ),
                                _iface("InterfacePhysical", "Ethernet2", "Customer LAN", None),
                                _iface(
                                    "InterfaceVirtual",
                                    f"Ethernet2.{vlan}",
                                    "Customer LAN (customer VLAN)",
                                    lan_address,
                                    dot1q=vlan,
                                    parent="Ethernet2",
                                ),
                            ]
                        },
                    }
                }
            ]
        },
        "RoutingBGPSession": {"edges": session_edges},
        "ServiceL3VpnSite": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "trading-london"},
                        "customer_subnet": {"node": {"prefix": {"value": customer_subnet}}},
                        "l3vpn": {
                            "node": {
                                "name": {"value": "trading-floor-vpn"},
                                "vpn_id": {"value": 100},
                            }
                        },
                    }
                }
            ]
        },
    }


def sdwan_edge_data(
    *,
    device_name: str = "treasury-branch-sdwan-hub-london-edge",
    platform: str = "cisco_viptela",
    location: str = "lon",
    site_name: str = "hub-london",
    site_role: str = "hub",
    lan_subnet: str = "10.250.10.0/24",
    lan_address: str = "10.250.10.1/24",
    service_name: str = "treasury-branch-sdwan",
    service_id: int = 100,
    vendor: str = "viptela",
    topology: str = "hub-spoke",
    tenant: str = "treasury-ops",
    sibling_sites: list[tuple[str, str, str]] | None = None,
) -> dict:
    """Build a sample SD-WAN edge transform input payload.

    Args:
        device_name: Hostname of the edge device.
        platform: Platform name string (e.g. ``"cisco_viptela"``).
        location: Location shortname (e.g. ``"lon"``).
        site_name: SD-WAN site name (e.g. ``"hub-london"``).
        site_role: Site role (e.g. ``"hub"`` or ``"spoke"``).
        lan_subnet: LAN subnet prefix (e.g. ``"10.250.10.0/24"``).
        lan_address: LAN interface address in CIDR (e.g. ``"10.250.10.1/24"``).
        service_name: SD-WAN service name.
        service_id: Numeric service identifier used for site-id and system-ip.
        vendor: SD-WAN vendor string (e.g. ``"viptela"``).
        topology: Overlay topology type (e.g. ``"hub-spoke"`` or ``"full-mesh"``).
        tenant: Tenant name for organization-name.
        sibling_sites: ``[(name, location_shortname, lan_subnet), ...]``
            entries representing peer sites in the same service.

    Returns:
        Dict shaped like the ``sdwan_edge`` GraphQL query response.
    """
    sibling_sites = sibling_sites or []
    sibling_edges = [
        {
            "node": {
                "name": {"value": sn},
                "location": {"node": {"shortname": {"value": loc}}},
                "lan_subnet": {"node": {"prefix": {"value": lan}}},
            }
        }
        for sn, loc, lan in sibling_sites
    ]
    return {
        "DcimDevice": {
            "edges": [
                {
                    "node": {
                        "id": "edge-id",
                        "name": {"value": device_name},
                        "platform": {"node": {"name": {"value": platform}}},
                        "location": {
                            "node": {
                                "name": {"value": location.upper()},
                                "shortname": {"value": location},
                            }
                        },
                    }
                }
            ]
        },
        "ServiceSdwanSite": {
            "edges": [
                {
                    "node": {
                        "id": "site-id",
                        "name": {"value": site_name},
                        "role": {"value": site_role},
                        "lan_subnet": {"node": {"prefix": {"value": lan_subnet}}},
                        "lan_address": {"node": {"address": {"value": lan_address}}},
                        "sdwan": {
                            "node": {
                                "name": {"value": service_name},
                                "service_id": {"value": service_id},
                                "vendor": {"value": vendor},
                                "topology": {"value": topology},
                                "tenant": {"node": {"name": {"value": tenant}}},
                                "sites": {"edges": sibling_edges},
                            }
                        },
                    }
                }
            ]
        },
    }
