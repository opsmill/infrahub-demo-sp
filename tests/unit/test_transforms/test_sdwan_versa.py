"""Snapshot-shape tests for the Versa (VOS) SD-WAN transform."""

from __future__ import annotations

import pytest

from transforms.sdwan_versa import SdwanVersa

from .fixtures import sdwan_edge_data


async def _render(data: dict) -> str:
    return await SdwanVersa.__new__(SdwanVersa).transform(data)


@pytest.mark.asyncio
async def test_renders_org_services_block() -> None:
    output = await _render(
        sdwan_edge_data(
            vendor="versa",
            platform="versa_flexvnf",
            tenant="treasury-ops",
        )
    )
    assert "set orgs org-services treasury-ops" in output


@pytest.mark.asyncio
async def test_renders_lan_virtual_router() -> None:
    output = await _render(
        sdwan_edge_data(
            vendor="versa",
            platform="versa_flexvnf",
            lan_address="10.250.10.1/24",
        )
    )
    assert "virtual-router LAN" in output
    assert "10.250.10.1/24" in output


@pytest.mark.asyncio
async def test_renders_site_id_in_appliance_name() -> None:
    output = await _render(
        sdwan_edge_data(
            vendor="versa",
            platform="versa_flexvnf",
            service_id=100,
        )
    )
    assert "site-id 100" in output
