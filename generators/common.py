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
    prefix = await client.allocate_next_ip_prefix(
        pool,
        identifier=identifier,
        prefix_length=prefix_length,
        branch=branch,
    )
    # The pool allocation does not populate the mandatory `status` field;
    # set it so subsequent saves on this prefix succeed.
    if not prefix.status.value:
        prefix.status.value = "active"
        await prefix.save(allow_upsert=True)
    return prefix


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
