"""Unit tests for the shared generator helpers in `generators/common.py`."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import (
    allocate_prefix_from_pool,
    find_or_create_device,
    find_or_create_route_target,
    next_free_physical_interface,
)


@pytest.mark.asyncio
async def test_allocate_prefix_passes_status_active_in_data() -> None:
    """The pool allocator must set status=active in the create-mutation data.

    IpamPrefix's `status` is required — without this the allocation errors
    on schema validation before the prefix is ever materialised.
    """
    client = MagicMock()
    client.get = AsyncMock(return_value="pool-object")
    client.allocate_next_ip_prefix = AsyncMock(return_value="allocated-prefix")

    result = await allocate_prefix_from_pool(
        client=client,
        pool_name="pe_ce_pool",
        branch="main",
        identifier="vpn-1:site-A",
        prefix_length=30,
    )

    assert result == "allocated-prefix"
    client.get.assert_awaited_once_with(
        kind="CoreIPPrefixPool", name__value="pe_ce_pool", branch="main"
    )
    client.allocate_next_ip_prefix.assert_awaited_once_with(
        "pool-object",
        identifier="vpn-1:site-A",
        prefix_length=30,
        data={"status": "active"},
        branch="main",
    )


@pytest.mark.asyncio
async def test_allocate_prefix_with_pool_default_length() -> None:
    """Omitting prefix_length passes None — pool's default is used."""
    client = MagicMock()
    client.get = AsyncMock(return_value="pool")
    client.allocate_next_ip_prefix = AsyncMock(return_value="p")

    await allocate_prefix_from_pool(
        client=client, pool_name="loopback_pool", branch="main", identifier="dev-1"
    )

    call = client.allocate_next_ip_prefix.await_args
    assert call.kwargs["prefix_length"] is None


@pytest.mark.asyncio
async def test_find_or_create_route_target_returns_existing() -> None:
    """If a route-target exists, return it instead of creating a new one."""
    existing = MagicMock()
    client = MagicMock()
    client.filters = AsyncMock(return_value=[existing])
    client.create = AsyncMock()

    result = await find_or_create_route_target(client=client, name="65000:100", branch="main")

    assert result is existing
    client.create.assert_not_called()


@pytest.mark.asyncio
async def test_find_or_create_route_target_creates_when_absent() -> None:
    """Empty filter result triggers a create + save."""
    new_rt = MagicMock()
    new_rt.save = AsyncMock()
    client = MagicMock()
    client.filters = AsyncMock(return_value=[])
    client.create = AsyncMock(return_value=new_rt)

    result = await find_or_create_route_target(client=client, name="65000:200", branch="main")

    assert result is new_rt
    client.create.assert_awaited_once_with(kind="IpamRouteTarget", branch="main", name="65000:200")
    new_rt.save.assert_awaited_once_with(allow_upsert=True)


@pytest.mark.asyncio
async def test_next_free_physical_interface_returns_lowest_numbered() -> None:
    """Sort by .name.value so allocation is deterministic across reruns."""
    iface_eth3 = MagicMock()
    iface_eth3.name.value = "Ethernet3"
    iface_eth1 = MagicMock()
    iface_eth1.name.value = "Ethernet1"
    iface_eth2 = MagicMock()
    iface_eth2.name.value = "Ethernet2"

    client = MagicMock()
    client.filters = AsyncMock(return_value=[iface_eth3, iface_eth1, iface_eth2])

    result = await next_free_physical_interface(
        client=client, device_name="pe-lon-arista", branch="main"
    )

    assert result is iface_eth1
    client.filters.assert_awaited_once_with(
        kind="InterfacePhysical",
        device__name__value="pe-lon-arista",
        status__value="free",
        branch="main",
    )


@pytest.mark.asyncio
async def test_next_free_physical_interface_raises_when_none_available() -> None:
    """Empty interface pool must raise, not return None — callers can't recover."""
    client = MagicMock()
    client.filters = AsyncMock(return_value=[])

    with pytest.raises(RuntimeError, match="No free physical interface on pe-foo"):
        await next_free_physical_interface(client=client, device_name="pe-foo", branch="main")


@pytest.mark.asyncio
async def test_find_or_create_device_returns_existing() -> None:
    """Idempotency: an existing DcimDevice with the same name is returned unchanged."""
    existing = MagicMock()
    client = MagicMock()
    client.filters = AsyncMock(return_value=[existing])
    client.create = AsyncMock()

    result = await find_or_create_device(
        client=client,
        name="acme-london-edge",
        platform_name="cisco_viptela",
        device_type_name="cEdge-1000",
        location_hfid="lon",
        role="cpe",
        branch="main",
    )

    assert result is existing
    client.create.assert_not_called()


@pytest.mark.asyncio
async def test_find_or_create_device_creates_with_hfid_relations() -> None:
    """When absent, the new DcimDevice carries hfid-keyed platform/device_type/location."""
    new_dev = MagicMock()
    new_dev.save = AsyncMock()
    client = MagicMock()
    client.filters = AsyncMock(return_value=[])
    client.create = AsyncMock(return_value=new_dev)

    result = await find_or_create_device(
        client=client,
        name="acme-frankfurt-edge",
        platform_name="cisco_viptela",
        device_type_name="cEdge-1000",
        location_hfid="fra",
        role="cpe",
        branch="main",
    )

    assert result is new_dev
    kwargs = client.create.await_args.kwargs
    assert kwargs["kind"] == "DcimDevice"
    assert kwargs["name"] == "acme-frankfurt-edge"
    assert kwargs["role"] == "cpe"
    assert kwargs["status"] == "active"
    assert kwargs["platform"] == {"hfid": ["cisco_viptela"]}
    assert kwargs["device_type"] == {"hfid": ["cEdge-1000"]}
    assert kwargs["location"] == {"hfid": ["fra"]}
    new_dev.save.assert_awaited_once_with(allow_upsert=True)
