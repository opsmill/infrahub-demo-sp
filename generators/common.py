"""Shared helpers for Infrahub generators.

These functions encapsulate Infrahub SDK calls that allocate resources
from pools and look up objects by deterministic keys.
"""

from __future__ import annotations

from typing import Any, cast

from infrahub_sdk.client import InfrahubClient


async def allocate_prefix_from_pool(
    client: InfrahubClient,
    pool_name: str,
    branch: str,
    identifier: str,
    *,
    prefix_length: int | None = None,
) -> Any:
    """Allocate the next free prefix from a CoreIPPrefixPool.

    Args:
        client: Active Infrahub SDK client.
        pool_name: Name of the CoreIPPrefixPool (e.g. ``pe_ce_pool``).
        branch: Branch on which to allocate.
        identifier: Unique identifier for this allocation (idempotency key).
        prefix_length: Override the pool default prefix length if set.

    Returns:
        The Infrahub node for the newly-allocated IpamPrefix.
    """
    pool: Any = await client.get(kind="CoreIPPrefixPool", name__value=pool_name, branch=branch)
    # ``status`` is mandatory on IpamPrefix; pass it via ``data`` so the
    # pool's create-mutation populates it during allocation.
    return await client.allocate_next_ip_prefix(
        pool,
        identifier=identifier,
        prefix_length=prefix_length,
        data={"status": "active"},
        branch=branch,
    )


async def find_or_create_route_target(
    client: InfrahubClient,
    name: str,
    branch: str,
) -> Any:
    """Return the IpamRouteTarget with this name, creating it if absent."""
    rt = await client.filters(kind="IpamRouteTarget", name__value=name, branch=branch)
    if rt:
        return rt[0]
    obj = await client.create(kind="IpamRouteTarget", branch=branch, name=name)
    await obj.save(allow_upsert=True)
    return obj


async def next_free_physical_interface(
    client: InfrahubClient,
    device_name: str,
    branch: str,
) -> Any:
    """Return the lowest-numbered Physical interface on a device with status=free.

    The base-library ``Interface`` generic has a ``status`` enum that
    includes ``free`` as a choice; the role enum does not. We allocate
    based on status to avoid extending the base role enum.

    Raises:
        RuntimeError: If no free interface is available.
    """
    candidates = await client.filters(
        kind="InterfacePhysical",
        device__name__value=device_name,
        status__value="free",
        branch=branch,
    )
    if not candidates:
        raise RuntimeError(f"No free physical interface on {device_name}")
    candidates.sort(key=lambda c: cast(Any, c.name).value)
    return candidates[0]


async def find_or_create_device(
    client: InfrahubClient,
    name: str,
    platform_name: str,
    device_type_name: str,
    manufacturer_name: str,
    location_hfid: str,
    role: str,
    branch: str,
) -> Any:
    """Return the DcimDevice with this name, creating it if absent.

    Used by the SD-WAN generator to materialise one edge device per site.
    The device is created with role=cpe, status=active, and bound to the
    site's LocationSite. Idempotent: if a device with this name already
    exists, it is returned unchanged.

    Args:
        client: Active Infrahub SDK client.
        name: Device name (typically ``<service>-<site>-edge``).
        platform_name: HFID of the DcimPlatform (e.g. ``cisco_viptela``).
        device_type_name: HFID of the DcimDeviceType (e.g. ``cEdge-1000``).
        manufacturer_name: HFID of the OrganizationManufacturer.
        location_hfid: HFID of the LocationSite (e.g. ``lon``).
        role: Role enum value (e.g. ``cpe``).
        branch: Branch on which to create.

    Returns:
        The Infrahub node for the device.
    """
    existing = await client.filters(kind="DcimDevice", name__value=name, branch=branch)
    if existing:
        return existing[0]
    device = await client.create(
        kind="DcimDevice",
        branch=branch,
        name=name,
        role=role,
        status="active",
        platform={"hfid": [platform_name]},
        device_type=[device_type_name, manufacturer_name],
        location={"hfid": [location_hfid]},
    )
    await device.save(allow_upsert=True)
    return device
