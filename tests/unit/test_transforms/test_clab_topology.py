"""Render-and-assert test for the clab topology template.

The clab transform derives backbone links by pairing the ``core`` interfaces
of lab-deployable PEs that share a /31 prefix (there is no explicit
interface-peering relationship in the schema). These fixtures therefore carry
per-interface IP addresses so the derivation has something to pair.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from transforms.clab_topology import ClabTopology


def _platform(name: str, clab_os: str | None) -> dict:
    """Build a ``platform`` relationship node with the given name and clab OS."""
    return {"node": {"name": {"value": name}, "containerlab_os": {"value": clab_os}}}


def _core_iface(name: str, address: str) -> dict:
    """Build a ``core``-role InterfacePhysical edge carrying a single address."""
    return {
        "node": {
            "__typename": "InterfacePhysical",
            "name": {"value": name},
            "role": {"value": "core"},
            "description": {"value": ""},
            "ip_addresses": {"edges": [{"node": {"address": {"value": address}}}]},
        }
    }


# Mixed dataset shaped like the ISP overlay: one Arista cEOS PE, one Cisco PE
# (no clab image), one Nokia PE substituted to SR Linux. The Arista↔Nokia pair
# shares 10.1.0.4/31 so exactly one backbone link is lab-deployable; the
# Arista↔Cisco /31 (10.1.0.0/31) yields no link because Cisco is not labbed.
FIXTURE = {
    "TopologyMplsBackbone": {
        "edges": [
            {
                "node": {
                    "name": {"value": "mpls-backbone-1"},
                    "pes": {
                        "edges": [
                            {
                                "node": {
                                    "id": "1",
                                    "name": {"value": "pe-lon-arista"},
                                    "platform": _platform("arista_eos", "ceos"),
                                    "interfaces": {
                                        "edges": [
                                            _core_iface("Ethernet1", "10.1.0.0/31"),
                                            _core_iface("Ethernet3", "10.1.0.4/31"),
                                        ]
                                    },
                                }
                            },
                            {
                                "node": {
                                    "id": "2",
                                    "name": {"value": "pe-fra-cisco"},
                                    "platform": _platform("cisco_iosxr", None),
                                    "interfaces": {
                                        "edges": [_core_iface("Ethernet1", "10.1.0.1/31")]
                                    },
                                }
                            },
                            {
                                "node": {
                                    "id": "4",
                                    "name": {"value": "pe-par-nokia"},
                                    "platform": _platform("nokia_sros", "srl"),
                                    "interfaces": {
                                        "edges": [_core_iface("Ethernet1", "10.1.0.5/31")]
                                    },
                                }
                            },
                        ]
                    },
                }
            }
        ]
    },
    "ServiceL3VpnSite": {"edges": []},
    "InterfaceVirtual": {"edges": []},
}


def _site(
    name: str,
    pe_name: str,
    pe_platform: tuple[str, str | None],
    pe_iface: str,
    *,
    ce_name: str | None = None,
    ce_platform: tuple[str, str | None] = ("arista_eos", "ceos"),
    ce_iface: str = "Ethernet1",
    ce_private_iface: str | None = "Ethernet2",
) -> dict:
    """Build a ServiceL3VpnSite edge.

    Args:
        name: Site name.
        pe_name: PE device name.
        pe_platform: ``(platform name, containerlab_os)`` for the PE.
        pe_iface: PE port allocated to this site by the L3VPN generator.
        ce_name: CE device name, or ``None`` for an unmanaged CE.
        ce_platform: ``(platform name, containerlab_os)`` for the CE.
        ce_iface: CE port facing the PE.

    Returns:
        A single ``edges`` entry shaped like the clab query result.
    """
    node: dict = {
        "name": {"value": name},
        "pe_device": {"node": {"name": {"value": pe_name}, "platform": _platform(*pe_platform)}},
        "pe_interface": {"node": {"name": {"value": pe_iface}}},
        "pe_address": {"node": {"address": {"value": "10.100.0.1/30"}}},
        "ce_address": {"node": {"address": {"value": "10.100.0.2/30"}}},
        "ce_device": {"node": None},
        "ce_interface": {"node": None},
        "ce_private_interface": {"node": None},
        "l3vpn": {"node": {"name": {"value": "trading-floor-vpn"}}},
    }
    if ce_name:
        node["ce_device"] = {
            "node": {"name": {"value": ce_name}, "platform": _platform(*ce_platform)}
        }
        node["ce_interface"] = {"node": {"name": {"value": ce_iface}}}
        node["ce_private_interface"] = (
            {"node": {"name": {"value": ce_private_iface}}} if ce_private_iface else {"node": None}
        )
    return {"node": node}


def _subif(device: str, vlan: int, gateway: str, parent: str = "Ethernet2") -> dict:
    """Build a tagged sub-interface edge as the clab query returns it."""
    return {
        "node": {
            "name": {"value": f"{parent}.{vlan}"},
            "dot1q_id": {"value": vlan},
            "device": {"node": {"name": {"value": device}}},
            "parent_interface": {"node": {"name": {"value": parent}}},
            "ip_addresses": {"edges": [{"node": {"address": {"value": gateway}}}]},
        }
    }


def _fixture_with_sites() -> dict:
    """Fixture extended with two PE-CE sites (Arista cEOS + Nokia SR Linux)."""
    fixture = copy.deepcopy(FIXTURE)
    fixture["ServiceL3VpnSite"] = {
        "edges": [
            _site(
                "london",
                "pe-lon-arista",
                ("arista_eos", "ceos"),
                "Ethernet4",
                ce_name="ce-trading-lon",
            ),
            _site(
                "paris",
                "pe-par-nokia",
                ("nokia_sros", "srl"),
                "Ethernet4",
                ce_name="ce-trading-par",
                ce_platform=("nokia_sros", "srl"),
            ),
        ]
    }
    fixture["InterfaceVirtual"] = {
        "edges": [
            _subif("ce-trading-lon", 100, "10.200.10.1/24"),
            _subif("ce-trading-par", 200, "10.200.20.1/24"),
        ]
    }
    return fixture


def _all_arista_fixture() -> dict:
    """Financial-shaped fixture: four all-cEOS PEs in a ring (four /31 links)."""
    links = [
        ("pe-01", "Ethernet1", "pe-02", "Ethernet1", "10.1.0.0"),
        ("pe-02", "Ethernet2", "pe-03", "Ethernet1", "10.1.0.2"),
        ("pe-03", "Ethernet2", "pe-04", "Ethernet1", "10.1.0.4"),
        ("pe-04", "Ethernet2", "pe-01", "Ethernet2", "10.1.0.6"),
    ]
    ifaces: dict[str, list[dict]] = {f"pe-0{n}": [] for n in range(1, 5)}
    for a_dev, a_if, b_dev, b_if, base in links:
        octet = int(base.rsplit(".", 1)[1])
        ifaces[a_dev].append(_core_iface(a_if, f"10.1.0.{octet}/31"))
        ifaces[b_dev].append(_core_iface(b_if, f"10.1.0.{octet + 1}/31"))
    return {
        "TopologyMplsBackbone": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "mpls-backbone-1"},
                        "pes": {
                            "edges": [
                                {
                                    "node": {
                                        "id": str(n),
                                        "name": {"value": f"pe-0{n}"},
                                        "platform": _platform("arista_eos", "ceos"),
                                        "interfaces": {"edges": ifaces[f"pe-0{n}"]},
                                    }
                                }
                                for n in range(1, 5)
                            ]
                        },
                    }
                }
            ]
        },
        "ServiceL3VpnSite": {"edges": []},
        "InterfaceVirtual": {"edges": []},
    }


def _link_strings(parsed: dict) -> list[str]:
    """Return the rendered topology links as strings for substring assertions."""
    return [str(link) for link in parsed["topology"]["links"]]


@pytest.mark.asyncio
async def test_includes_labbed_pes_only() -> None:
    """Lab includes Arista cEOS + Nokia SR Linux; excludes non-labbed platforms
    (this fixture carries a Cisco PE, which has no clab image)."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(FIXTURE)
    parsed = yaml.safe_load(rendered)
    nodes = parsed["topology"]["nodes"]
    assert "pe-lon-arista" in nodes
    assert "pe-par-nokia" in nodes
    assert "pe-fra-cisco" not in nodes


@pytest.mark.asyncio
async def test_nokia_substitutes_to_srl() -> None:
    """Nokia PE uses kind=srl (SR Linux) not sros."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(FIXTURE)
    parsed = yaml.safe_load(rendered)
    assert parsed["topology"]["nodes"]["pe-par-nokia"]["kind"] == "srl"


@pytest.mark.asyncio
async def test_renders_backbone_link_between_arista_and_nokia() -> None:
    """The one lab-deployable backbone link connects Arista and Nokia; the
    Arista↔Cisco /31 yields no link (Cisco has no clab image)."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(FIXTURE)
    parsed = yaml.safe_load(rendered)
    links = parsed["topology"]["links"]
    assert len(links) == 1
    assert any(("pe-lon-arista" in str(link) and "pe-par-nokia" in str(link)) for link in links)
    assert not any("pe-fra-cisco" in str(link) for link in links)


@pytest.mark.asyncio
async def test_all_arista_backbone_renders_every_link() -> None:
    """A single-vendor cEOS backbone renders one link per /31 (here a 4-PE ring)."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(_all_arista_fixture())
    parsed = yaml.safe_load(rendered)
    links = _link_strings(parsed)
    assert len(links) == 4
    # Every PE appears as a lab node and the ring closes (pe-01 <-> pe-04).
    assert set(parsed["topology"]["nodes"]) == {"pe-01", "pe-02", "pe-03", "pe-04"}
    assert any("pe-04" in s and "pe-01" in s for s in links)


@pytest.mark.asyncio
async def test_mgmt_subnet_does_not_overlap_sp_demo_network() -> None:
    """mgmt.ipv4-subnet sits outside 172.20.0.0/16 (sp-demo compose network)."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(FIXTURE)
    parsed = yaml.safe_load(rendered)
    assert "mgmt" in parsed, "mgmt block must be set to avoid clab's default 172.20.20.0/24"
    subnet = parsed["mgmt"]["ipv4-subnet"]
    assert not subnet.startswith("172.20."), (
        f"clab mgmt subnet {subnet} overlaps the sp-demo compose network 172.20.0.0/16"
    )


@pytest.mark.asyncio
async def test_arista_uses_eth_naming_in_clab_links() -> None:
    """cEOS link endpoints must use ethN (clab maps eth<N> ↔ Ethernet<N>)."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(_fixture_with_sites())
    parsed = yaml.safe_load(rendered)
    # Backbone link arista-nokia
    assert any("pe-lon-arista:eth3" in s for s in _link_strings(parsed))
    # PE-CE link from Arista — Ethernet4 → eth4, CE Ethernet1 → eth1
    assert any(
        "pe-lon-arista:eth4" in s and "ce-trading-lon:eth1" in s for s in _link_strings(parsed)
    )
    # No raw EthernetN should appear anywhere
    for s in _link_strings(parsed):
        assert "Ethernet" not in s, f"raw Ethernet name leaked into clab link: {s}"


@pytest.mark.asyncio
async def test_srl_uses_ethernet_1_naming_in_clab_links() -> None:
    """SR Linux link endpoints must match the ethernet-1/N pattern."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(_fixture_with_sites())
    parsed = yaml.safe_load(rendered)
    # Backbone link nokia side
    assert any("pe-par-nokia:ethernet-1/1" in s for s in _link_strings(parsed))
    # PE-CE link from Nokia — Ethernet4 → ethernet-1/4, CE Ethernet1 → ethernet-1/1
    assert any(
        "pe-par-nokia:ethernet-1/4" in s and "ce-trading-par:ethernet-1/1" in s
        for s in _link_strings(parsed)
    )


@pytest.mark.asyncio
async def test_each_labbed_pe_has_startup_config_path() -> None:
    """Every labbed PE node references a per-device startup-config file."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(FIXTURE)
    parsed = yaml.safe_load(rendered)
    nodes = parsed["topology"]["nodes"]
    assert nodes["pe-lon-arista"]["startup-config"] == "devices/pe-lon-arista.cfg"
    assert nodes["pe-par-nokia"]["startup-config"] == "devices/pe-par-nokia.cfg"


@pytest.mark.asyncio
async def test_ces_boot_from_a_rendered_config_like_pes() -> None:
    """CEs are real routers, not netshoot stand-ins.

    They boot the same way the PEs do — their own container kind plus a
    startup-config artifact — which is what lets the PE-CE eBGP session
    actually establish in the lab.
    """
    rendered = await ClabTopology.__new__(ClabTopology).transform(_fixture_with_sites())
    nodes = yaml.safe_load(rendered)["topology"]["nodes"]
    assert nodes["ce-trading-lon"] == {
        "kind": "ceos",
        "startup-config": "devices/ce-trading-lon.cfg",
    }
    assert nodes["ce-trading-par"] == {
        "kind": "srl",
        "startup-config": "devices/ce-trading-par.cfg",
    }


@pytest.mark.asyncio
async def test_site_with_unmanaged_ce_renders_no_ce_node_or_link() -> None:
    """A site without a ce_device contributes neither a node nor a PE-CE link.

    Sites whose CE the customer owns are still valid; there is just nothing for
    the lab to boot, and half a link would break `clab deploy`.
    """
    fixture = copy.deepcopy(FIXTURE)
    fixture["ServiceL3VpnSite"] = {
        "edges": [_site("london", "pe-lon-arista", ("arista_eos", "ceos"), "Ethernet4")]
    }
    parsed = yaml.safe_load(await ClabTopology.__new__(ClabTopology).transform(fixture))
    assert not [name for name in parsed["topology"]["nodes"] if name.startswith("ce-")]
    assert not [s for s in _link_strings(parsed) if "eth4" in s]


@pytest.mark.asyncio
async def test_site_awaiting_the_generator_renders_no_ce_link() -> None:
    """No PE port yet (generator hasn't run) → the CE is left out entirely."""
    fixture = copy.deepcopy(FIXTURE)
    site = _site(
        "london", "pe-lon-arista", ("arista_eos", "ceos"), "Ethernet4", ce_name="ce-trading-lon"
    )
    site["node"]["pe_interface"] = {"node": None}
    fixture["ServiceL3VpnSite"] = {"edges": [site]}
    parsed = yaml.safe_load(await ClabTopology.__new__(ClabTopology).transform(fixture))
    assert "ce-trading-lon" not in parsed["topology"]["nodes"]


@pytest.mark.asyncio
async def test_lan_host_attached_to_each_ce_private_side() -> None:
    """Each CE LAN port gets a host, tagging with the CE's own VLAN.

    Without one the port has no carrier: it stays down, the dot1q sub-interface
    sits lowerlayerdown, and the customer prefix is never advertised because a
    down interface contributes no connected route for `network` to match.
    """
    parsed = yaml.safe_load(
        await ClabTopology.__new__(ClabTopology).transform(_fixture_with_sites())
    )
    nodes = parsed["topology"]["nodes"]
    assert nodes["host-ce-trading-lon"]["kind"] == "linux"
    execs = " ".join(nodes["host-ce-trading-lon"]["exec"])
    assert "type vlan id 100" in execs, execs
    assert "ip addr add 10.200.10.10/24 dev eth1.100" in execs, execs
    # Gateway is the CE sub-interface address, and the route must be idempotent.
    assert "ip route replace default via 10.200.10.1" in execs, execs


@pytest.mark.asyncio
async def test_lan_link_wires_host_to_the_ce_private_port() -> None:
    """The LAN link must land on the CE's private port, not the PE-facing one."""
    parsed = yaml.safe_load(
        await ClabTopology.__new__(ClabTopology).transform(_fixture_with_sites())
    )
    links = _link_strings(parsed)
    assert any("ce-trading-lon:eth2" in s and "host-ce-trading-lon:eth1" in s for s in links), links
    # The PE-facing link is still eth1 on the CE.
    assert any("ce-trading-lon:eth1" in s and "pe-lon-arista" in s for s in links), links


@pytest.mark.asyncio
async def test_no_lan_host_without_an_allocated_vlan() -> None:
    """A CE whose VLAN has not been allocated yet gets no host.

    Rendering one would mean guessing the tag, and a mismatched tag is worse
    than an absent host: the link would come up and silently blackhole.
    """
    fixture = _fixture_with_sites()
    fixture["InterfaceVirtual"] = {"edges": []}
    parsed = yaml.safe_load(await ClabTopology.__new__(ClabTopology).transform(fixture))
    assert not [n for n in parsed["topology"]["nodes"] if n.startswith("host-")]
