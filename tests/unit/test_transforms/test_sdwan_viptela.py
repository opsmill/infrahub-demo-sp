"""Snapshot-shape tests for the Viptela (Cisco IOS-XE SD-WAN) transform."""

from __future__ import annotations

import pytest

from transforms.sdwan_viptela import SdwanViptela

from .fixtures import sdwan_edge_data


async def _render(data: dict) -> str:
    return await SdwanViptela.__new__(SdwanViptela).transform(data)


@pytest.mark.asyncio
async def test_renders_hostname_and_system_block() -> None:
    output = await _render(sdwan_edge_data())
    assert "host-name treasury-branch-sdwan-hub-london-edge" in output
    assert "site-id 100" in output


@pytest.mark.asyncio
async def test_renders_vpn1_lan_address() -> None:
    output = await _render(sdwan_edge_data(lan_address="10.250.10.1/24"))
    assert "vpn 1" in output
    assert "ip address 10.250.10.1/24" in output


@pytest.mark.asyncio
async def test_renders_topology_comment_for_full_mesh() -> None:
    output = await _render(sdwan_edge_data(topology="full-mesh"))
    assert "full-mesh" in output


@pytest.mark.asyncio
async def test_renders_organization_name_from_tenant() -> None:
    output = await _render(sdwan_edge_data(tenant="treasury-ops"))
    assert 'organization-name "treasury-ops"' in output
