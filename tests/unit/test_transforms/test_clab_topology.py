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
