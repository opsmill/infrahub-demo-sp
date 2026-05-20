"""Render-and-assert test for the clab topology template."""

from __future__ import annotations

import pytest
import yaml

from transforms.clab_topology import ClabTopology

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
                                    "platform": {
                                        "node": {
                                            "name": {"value": "arista_eos"},
                                            "containerlab_os": {"value": "ceos"},
                                        }
                                    },
                                    "interfaces": {"edges": []},
                                }
                            },
                            {
                                "node": {
                                    "id": "2",
                                    "name": {"value": "pe-fra-cisco"},
                                    "platform": {
                                        "node": {
                                            "name": {"value": "cisco_iosxr"},
                                            "containerlab_os": {"value": None},
                                        }
                                    },
                                    "interfaces": {"edges": []},
                                }
                            },
                            {
                                "node": {
                                    "id": "4",
                                    "name": {"value": "pe-par-nokia"},
                                    "platform": {
                                        "node": {
                                            "name": {"value": "nokia_sros"},
                                            "containerlab_os": {"value": "srl"},
                                        }
                                    },
                                    "interfaces": {"edges": []},
                                }
                            },
                        ]
                    },
                }
            }
        ]
    },
    "ServiceL3VpnSite": {"edges": []},
}


def _fixture_with_sites() -> dict:
    """Fixture extended with two PE-CE sites (Arista cEOS + Nokia SR Linux)."""
    import copy

    fixture = copy.deepcopy(FIXTURE)
    fixture["ServiceL3VpnSite"] = {
        "edges": [
            {
                "node": {
                    "name": {"value": "london"},
                    "pe_device": {
                        "node": {
                            "name": {"value": "pe-lon-arista"},
                            "platform": {
                                "node": {
                                    "name": {"value": "arista_eos"},
                                    "containerlab_os": {"value": "ceos"},
                                }
                            },
                        }
                    },
                    "pe_interface": {"node": {"name": {"value": "Ethernet4"}}},
                    "pe_address": {"node": {"address": {"value": "10.100.0.1/30"}}},
                    "ce_address": {"node": {"address": {"value": "10.100.0.2/30"}}},
                    "l3vpn": {"node": {"name": {"value": "trading-floor-vpn"}}},
                }
            },
            {
                "node": {
                    "name": {"value": "paris"},
                    "pe_device": {
                        "node": {
                            "name": {"value": "pe-par-nokia"},
                            "platform": {
                                "node": {
                                    "name": {"value": "nokia_sros"},
                                    "containerlab_os": {"value": "srl"},
                                }
                            },
                        }
                    },
                    "pe_interface": {"node": {"name": {"value": "Ethernet4"}}},
                    "pe_address": {"node": {"address": {"value": "10.100.4.1/30"}}},
                    "ce_address": {"node": {"address": {"value": "10.100.4.2/30"}}},
                    "l3vpn": {"node": {"name": {"value": "trading-floor-vpn"}}},
                }
            },
        ]
    }
    return fixture


@pytest.mark.asyncio
async def test_includes_labbed_pes_only() -> None:
    """Lab includes Arista cEOS + Nokia SR Linux; excludes Cisco / Juniper."""
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
    """A single backbone link connects Arista and Nokia."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(FIXTURE)
    parsed = yaml.safe_load(rendered)
    links = parsed["topology"]["links"]
    assert any(("pe-lon-arista" in str(link) and "pe-par-nokia" in str(link)) for link in links)


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


def _link_strings(parsed: dict) -> list[str]:
    return [str(link) for link in parsed["topology"]["links"]]


@pytest.mark.asyncio
async def test_arista_uses_eth_naming_in_clab_links() -> None:
    """cEOS link endpoints must use ethN (clab maps eth<N> ↔ Ethernet<N>)."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(_fixture_with_sites())
    parsed = yaml.safe_load(rendered)
    # Backbone link arista-nokia
    assert any("pe-lon-arista:eth3" in s for s in _link_strings(parsed))
    # PE-CE link from Arista — Ethernet4 → eth4
    assert any("pe-lon-arista:eth4" in s for s in _link_strings(parsed))
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
    # PE-CE link from Nokia — Ethernet4 → ethernet-1/4
    assert any("pe-par-nokia:ethernet-1/4" in s for s in _link_strings(parsed))


@pytest.mark.asyncio
async def test_each_labbed_pe_has_startup_config_path() -> None:
    """Every labbed PE node references a per-device startup-config file."""
    rendered = await ClabTopology.__new__(ClabTopology).transform(FIXTURE)
    parsed = yaml.safe_load(rendered)
    nodes = parsed["topology"]["nodes"]
    assert nodes["pe-lon-arista"]["startup-config"] == "devices/pe-lon-arista.cfg"
    assert nodes["pe-par-nokia"]["startup-config"] == "devices/pe-par-nokia.cfg"
