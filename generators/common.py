"""Shared helpers for Infrahub generators.

These functions encapsulate Infrahub SDK calls that allocate resources
from pools and look up objects by deterministic keys.
"""

from __future__ import annotations

from typing import Any, cast

from infrahub_sdk.client import InfrahubClient

# Provider-owned address space (PE-CE /30s, SD-WAN LAN addresses, every pool in
# objects/50_pools.yml) lives here.
#
# Customer space does not: each L3VPN's customer prefixes are created in a
# namespace of their own, named `vrf-<vpn name>`, because IpamPrefix and
# IpamIPAddress are unique on [value, ip_namespace] and two customers may
# legitimately use the same private prefix — the financial and isp datasets each
# hand 10.200.10.0/24 to a different customer. That namespace is created by
# whoever creates the prefix (the datasets declare it, the Service Catalog and
# scripts/smoke_create_l3vpn.py create it inline); the L3VPN generator only
# *reads* it, off the site's own customer_subnet.
#
# The generator deliberately does not create or re-save it. Generators run under
# `delete_unused_nodes=True`, so every node they save joins their tracking group
# and any run that then fails to save it makes the reaper delete it — for a
# namespace that still holds live customer prefixes the delete fails, and the
# whole generator run dies on an unreadable IpamNamespaceDelete GraphQL error.
# Observed on a live server. Reading the namespace keeps it out of that set.
DEFAULT_IP_NAMESPACE = "default"


async def touch(node: Any) -> Any:
    """Re-save a node the generator owns so the tracking reaper keeps it.

    Generators run inside ``start_tracking(..., delete_unused_nodes=True)``: any
    node a previous run created that this run does not save is treated as unused
    and deleted. Adopting an existing node and returning it without a save is
    therefore not a no-op — it orphans the node, and the next run finds it gone.
    Every "found it, reuse it" path has to end here.

    Exists so that invariant has one name and one place instead of a
    hand-written ``save(allow_upsert=True)`` at each adopt site; forgetting one
    is what made ``invoke bootstrap`` destructive on a populated database. See
    the module docstring of generators/generate_l3vpn.py.

    Args:
        node: The Infrahub node to re-save unchanged.

    Returns:
        The same node, so callers can ``return await touch(existing[0])``.
    """
    await node.save(allow_upsert=True)
    return node


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


async def allocate_asn_from_pool(
    client: InfrahubClient,
    pool_name: str,
    branch: str,
    *,
    name: str,
    organization_id: str,
    description: str | None = None,
) -> Any:
    """Create a RoutingAutonomousSystem whose ASN comes from a CoreNumberPool.

    Number pools are consumed by handing the pool node itself to the attribute
    on create; the server allocates the next free value and records it against
    the pool, so utilisation stays visible in the UI.

    This saves WITHOUT ``allow_upsert``. ``RoutingAutonomousSystem``'s
    human-friendly ID is ``[asn__value, name__value]``, and ``asn`` is what the
    pool assigns — so the HFID is different on every attempt and can never
    match an existing row. The server rejects that combination outright
    ("Attribute 'asn' is sourced from a CoreNumberPool and is part of this
    node's HFID"). Idempotency has to come from the caller instead: look the AS
    up by its stable ``name`` first and only call this when it is absent.

    Args:
        client: Active Infrahub SDK client.
        pool_name: Name of the CoreNumberPool (e.g. ``customer_asn_pool``).
        branch: Branch on which to allocate.
        name: Name for the new autonomous system (its natural key).
        organization_id: Infrahub ID of the owning organization.
        description: Optional description for the new autonomous system.

    Returns:
        The Infrahub node for the newly-created RoutingAutonomousSystem.
    """
    pool: Any = await client.get(kind="CoreNumberPool", name__value=pool_name, branch=branch)
    autonomous_system = await client.create(
        kind="RoutingAutonomousSystem",
        branch=branch,
        name=name,
        asn=pool,
        description=description,
        organization={"id": organization_id},
    )
    await autonomous_system.save()
    return autonomous_system


async def find_or_create_route_target(
    client: InfrahubClient,
    name: str,
    branch: str,
) -> Any:
    """Return the IpamRouteTarget with this name, creating it if absent."""
    rt = await client.filters(kind="IpamRouteTarget", name__value=name, branch=branch)
    if rt:
        return await touch(rt[0])
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
        location_hfid: HFID of the LocationSite (e.g. ``lon``).
        role: Role enum value (e.g. ``cpe``).
        branch: Branch on which to create.

    Returns:
        The Infrahub node for the device.
    """
    existing = await client.filters(kind="DcimDevice", name__value=name, branch=branch)
    if existing:
        # Without the touch the SD-WAN edge devices are deleted on the
        # generator's second run.
        return await touch(existing[0])
    device = await client.create(
        kind="DcimDevice",
        branch=branch,
        name=name,
        role=role,
        status="active",
        platform={"hfid": [platform_name]},
        device_type={"hfid": [device_type_name]},
        location={"hfid": [location_hfid]},
    )
    await device.save(allow_upsert=True)
    return device


async def allocate_vlan_subinterface(
    client: InfrahubClient,
    branch: str,
    *,
    pool_name: str,
    parent: Any,
    device_name: str,
    parent_name: str,
    description: str,
) -> Any:
    """Create a dot1q sub-interface whose VLAN ID comes from a CoreNumberPool.

    The VLAN is allocated by handing the pool node to ``dot1q_id`` on create.
    Pools allocate on create only, which is why the VLAN lands on a
    sub-interface the generator makes rather than on the pre-provisioned parent
    port — there is nothing to allocate into on an object that already exists.

    Unlike the customer ASN, ``dot1q_id`` is not part of this node's
    human-friendly ID (``[device__name__value, name__value]``), so saving with
    ``allow_upsert`` is safe here.

    Args:
        client: Active Infrahub SDK client.
        branch: Branch on which to allocate.
        pool_name: Name of the CoreNumberPool holding the customer's VLANs.
        parent: The parent InterfacePhysical node.
        device_name: Name of the device the sub-interface belongs to.
        parent_name: Name of the parent port (e.g. ``Ethernet2``).
        description: Description for the new sub-interface.

    Returns:
        The InterfaceVirtual node, with ``dot1q_id`` populated by the pool.
    """
    pool: Any = await client.get(kind="CoreNumberPool", name__value=pool_name, branch=branch)
    # The name cannot be known before allocation (it embeds the VLAN), so create
    # with a placeholder, read back the assigned VLAN, then set the real name.
    sub: Any = await client.create(
        kind="InterfaceVirtual",
        branch=branch,
        name=f"{parent_name}.pending",
        description=description,
        status="active",
        role="access",
        mtu=1500,
        dot1q_id=pool,
        device={"hfid": [device_name]},
        parent_interface=parent,
    )
    await sub.save()
    vlan = int(sub.dot1q_id.value)
    sub.name.value = f"{parent_name}.{vlan}"
    await sub.save(allow_upsert=True)
    return sub
