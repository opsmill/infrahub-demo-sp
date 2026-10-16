"""PE onboarding generator.

Materialises everything a ``TopologyPeOnboarding`` request implies: the device,
its Loopback0 and loopback address, one core link per uplink (a /31 from
``backbone_p2p_pool`` with a port on each end), the IS-IS, LDP and MP-BGP
processes, the iBGP sessions to and from every other PE in the backbone, the
group memberships the artifact pipeline targets, and membership of the backbone
itself. Idempotent.

GENERATOR TRACKING: see generators/generate_l3vpn.py. The SDK runs
``generate()`` inside ``start_tracking(..., delete_unused_nodes=True)``, so every
node this generator owns is saved on every run; a "found it, return early" path
would let the reaper delete it on the next run.

OWNERSHIP: nodes that exist before the request and are shared with other
targets are not claimed. The port claimed on each uplink PE is saved with
``update_group_context=False`` (generators/common.py:
find_or_claim_core_interface), and the group and backbone memberships go
through ``add_relationships``, which never enters the tracking group. The trade
is that nothing un-claims that port when a request is deleted, and a request
whose uplinks change after generation is NOT supported: the port on the old
uplink stays converted to a core port. The tutorial tells operators to delete
the request and open a new branch instead.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from infrahub_sdk.generator import InfrahubGenerator

from .common import (
    allocate_prefix_from_pool,
    backbone_pe_loopbacks,
    find_or_claim_core_interface,
    isis_net_id,
    touch,
)

LOG = logging.getLogger(__name__)

LOOPBACK_POOL = "pe_loopback_pool"
P2P_POOL = "backbone_p2p_pool"
CORE_MTU = 9000
SPARE_PORT_COUNT = 2
MIN_UPLINKS = 2
ADDRESS_FAMILIES = ["vpnv4", "vpnv6"]
# platform name -> artifact target group, as declared in objects/55_groups.yml
PLATFORM_GROUP: dict[str, str] = {
    "arista_eos": "pe_arista_eos",
    "cisco_iosxr": "pe_cisco_iosxr",
    "juniper_junos": "pe_juniper_junos",
    "nokia_sros": "pe_nokia_sros",
}


class PeOnboardingGenerator(InfrahubGenerator):
    """Generator that materialises everything downstream of a TopologyPeOnboarding row."""

    data: dict[str, Any]

    async def generate(self, data: dict[str, Any] | None = None) -> None:
        """Materialise the PE for a single onboarding request.

        Args:
            data: Result of the ``pe_onboarding`` GraphQL query. Falls back to
                ``self.data`` when not supplied.

        Raises:
            RuntimeError: If the request names fewer than two uplinks, a
                platform with no artifact group, or a device name that another
                device already uses.
        """
        payload = data or self.data
        edges = payload.get("TopologyPeOnboarding", {}).get("edges", [])
        if not edges:
            LOG.warning("No TopologyPeOnboarding matched; nothing to generate")
            return
        request = edges[0]["node"]
        name = request["name"]["value"]
        uplinks = sorted({edge["node"]["name"]["value"] for edge in request["uplinks"]["edges"]})
        if len(uplinks) < MIN_UPLINKS:
            raise RuntimeError(f"PE onboarding {name!r} needs at least {MIN_UPLINKS} uplinks")
        backbone = request["backbone"]["node"]
        backbone_as_id: str = backbone["asn"]["node"]["id"]
        platform = request["platform"]["node"]["name"]["value"]
        if platform not in PLATFORM_GROUP:
            raise RuntimeError(f"No artifact group for platform {platform!r}")

        request_obj = await self.client.get(
            kind="TopologyPeOnboarding", id=request["id"], branch=self.branch
        )
        device = await self._ensure_device(request, request_obj, backbone_as_id)
        loopback_ip = await self._ensure_loopback(device, request["id"])
        # The SDK hands IP attributes back as ipaddress objects, not strings.
        await self._ensure_processes(device, backbone, str(loopback_ip.address.value))
        for index, uplink in enumerate(uplinks, start=1):
            await self._ensure_uplink(device, name, uplink, index, request["id"])
        await self._ensure_spare_interfaces(device, len(uplinks))
        await self._ensure_ibgp_mesh(device, name, loopback_ip, backbone["id"], backbone_as_id)
        await self._ensure_memberships(device, platform, backbone["id"])

        request_obj.status.value = "active"  # type: ignore[union-attr]
        await request_obj.save(allow_upsert=True)

    async def _ensure_device(
        self, request: dict[str, Any], request_obj: Any, backbone_as_id: str
    ) -> Any:
        """Create the PE device, or re-save it when a previous run already did.

        Not ``find_or_create_device`` from common.py: the PE also needs its
        backbone AS, which that helper (written for SD-WAN edges) has no slot for.

        A device with the request's name is adopted only when the request
        already links to it. Adopting any other device would put it in this
        generator's tracking group, and deleting the request would then delete
        a device the request never created. The new device is linked to the
        request as soon as it exists, so a run that fails later still leaves
        the link for the next run to recognise.

        Args:
            request: The TopologyPeOnboarding node from the query result.
            request_obj: The same request as an SDK node, which holds ``device``.
            backbone_as_id: Infrahub id of the backbone RoutingAutonomousSystem.

        Returns:
            The DcimDevice node.

        Raises:
            RuntimeError: If a device with the request's name exists and the
                request does not link to it.
        """
        name = request["name"]["value"]
        existing = await self.client.filters(
            kind="DcimDevice", name__value=name, branch=self.branch
        )
        if existing:
            if existing[0].id != request_obj.device.id:
                raise RuntimeError(
                    f"A device named {name!r} already exists and was not created by this request"
                )
            return await touch(existing[0])
        device = await self.client.create(
            kind="DcimDevice",
            branch=self.branch,
            name=name,
            role="pe",
            status="active",
            platform={"id": request["platform"]["node"]["id"]},
            device_type={"id": request["device_type"]["node"]["id"]},
            location={"id": request["location"]["node"]["id"]},
            asn={"id": backbone_as_id},
        )
        await device.save(allow_upsert=True)
        request_obj.device = device
        await request_obj.save(allow_upsert=True)
        return device

    async def _ensure_loopback(self, device: Any, request_id: str) -> Any:
        """Create Loopback0 and allocate its /32 from the loopback pool.

        Args:
            device: The new PE's DcimDevice node.
            request_id: Infrahub id of the request, the pool allocation's idempotency key.

        Returns:
            The IpamIPAddress node bound to Loopback0.
        """
        existing = await self.client.filters(
            kind="InterfaceVirtual",
            device__name__value=device.name.value,
            name__value="Loopback0",
            branch=self.branch,
        )
        if existing:
            iface = await touch(existing[0])
        else:
            iface = await self.client.create(
                kind="InterfaceVirtual",
                branch=self.branch,
                name="Loopback0",
                description="Router-ID / iBGP source",
                mtu=1500,
                status="active",
                role="management",
                device=device,
            )
            await iface.save(allow_upsert=True)

        pool: Any = await self.client.get(
            kind="CoreIPAddressPool", name__value=LOOPBACK_POOL, branch=self.branch
        )
        ip: Any = await self.client.allocate_next_ip_address(
            pool,
            identifier=f"pe-onboarding-{request_id}-loopback",
            data={"description": f"{device.name.value} loopback"},
            branch=self.branch,
        )
        ip.interface = iface
        await ip.save(allow_upsert=True)
        return ip

    async def _ensure_processes(self, device: Any, backbone: dict[str, Any], loopback: str) -> None:
        """Create or re-save the IS-IS, LDP and MP-BGP processes, all keyed by device.

        Args:
            device: The new PE's DcimDevice node.
            backbone: The TopologyMplsBackbone node from the query result.
            loopback: The PE's loopback address, with prefix length.
        """
        router_id = loopback.split("/")[0]
        area = backbone["isis_area"]["value"]
        rows: tuple[tuple[str, dict[str, Any]], ...] = (
            (
                "MplsIsisProcess",
                {
                    "description": "ISIS L2",
                    "area_id": area,
                    "level": backbone["isis_level"]["value"],
                    "net_id": isis_net_id(area, loopback),
                },
            ),
            ("MplsLdpProcess", {"description": "LDP", "router_id": router_id}),
            (
                "MplsBgpProcess",
                {
                    "description": "MP-BGP VPNv4/v6",
                    "router_id": router_id,
                    "address_families": ADDRESS_FAMILIES,
                },
            ),
        )
        for kind, fields in rows:
            process = await self.client.create(
                kind=kind, branch=self.branch, device=device, status="active", **fields
            )
            await process.save(allow_upsert=True)

    async def _ensure_uplink(
        self, device: Any, name: str, uplink: str, index: int, request_id: str
    ) -> None:
        """Allocate a /31 and wire Ethernet<index> on the new PE to a claimed port on ``uplink``.

        Args:
            device: The new PE's DcimDevice node.
            name: The new PE's name.
            uplink: Name of the existing PE at the far end.
            index: 1-based position of this uplink, which names the local port.
            request_id: Infrahub id of the request, part of the pool allocation key.
        """
        prefix = await allocate_prefix_from_pool(
            self.client,
            P2P_POOL,
            self.branch,
            identifier=f"pe-onboarding-{request_id}-{uplink}",
            prefix_length=31,
        )
        prefix.description.value = f"{name} <-> {uplink}"
        prefix.role.value = "technical"
        await prefix.save(allow_upsert=True)
        network = ipaddress.IPv4Network(str(prefix.prefix.value))

        local_iface = await self.client.create(
            kind="InterfacePhysical",
            branch=self.branch,
            name=f"Ethernet{index}",
            description=f"To {uplink}",
            mtu=CORE_MTU,
            status="active",
            role="core",
            device=device,
        )
        await local_iface.save(allow_upsert=True)
        remote_iface = await find_or_claim_core_interface(
            self.client, uplink, f"To {name}", self.branch
        )
        await self._ensure_address(f"{network[0]}/31", local_iface)
        await self._ensure_address(f"{network[1]}/31", remote_iface)

    async def _ensure_spare_interfaces(self, device: Any, uplink_count: int) -> None:
        """Provision two spare ports for services or later backbone links.

        Existing ports are re-saved unchanged: another generator may already
        have claimed them, and resetting them to free would allow double use.

        Args:
            device: The new PE's DcimDevice node.
            uplink_count: Number of ports reserved for this request's uplinks.
        """
        for index in range(uplink_count + 1, uplink_count + SPARE_PORT_COUNT + 1):
            name = f"Ethernet{index}"
            existing = await self.client.filters(
                kind="InterfacePhysical",
                device__name__value=device.name.value,
                name__value=name,
                branch=self.branch,
            )
            if existing:
                await touch(existing[0])
                continue
            iface = await self.client.create(
                kind="InterfacePhysical",
                branch=self.branch,
                name=name,
                description="Customer-facing (free)",
                mtu=1500,
                status="free",
                role="cust",
                device=device,
            )
            await iface.save(allow_upsert=True)

    async def _ensure_address(self, address: str, iface: Any) -> Any:
        """Create or re-save an IpamIPAddress bound to ``iface``.

        Args:
            address: Address with prefix length, in the default namespace.
            iface: The interface node the address belongs to.

        Returns:
            The IpamIPAddress node.
        """
        existing = await self.client.filters(
            kind="IpamIPAddress", address__value=address, branch=self.branch
        )
        if existing:
            ip = existing[0]
            ip.interface = iface
            return await touch(ip)
        ip = await self.client.create(
            kind="IpamIPAddress", branch=self.branch, address=address, interface=iface
        )
        await ip.save(allow_upsert=True)
        return ip

    async def _ensure_ibgp_mesh(
        self, device: Any, name: str, loopback_ip: Any, backbone_id: str, backbone_as_id: str
    ) -> None:
        """Create the iBGP session rows in both directions to every other PE of the backbone.

        The Arista template renders ``RoutingBGPSession(device__name__value: $device)``,
        so each existing PE needs its own row pointing at the new loopback or its
        configuration never gains the neighbour, and BackboneSessionCountCheck
        would fail the proposed change.

        Args:
            device: The new PE's DcimDevice node.
            name: The new PE's name.
            loopback_ip: The new PE's Loopback0 IpamIPAddress node.
            backbone_id: Infrahub id of the TopologyMplsBackbone whose PEs to peer with.
            backbone_as_id: Infrahub id of the backbone RoutingAutonomousSystem.
        """
        loopbacks = await backbone_pe_loopbacks(self.client, self.branch, backbone_id)
        for peer_name, (peer_id, peer_ip_id, _) in sorted(loopbacks.items()):
            if peer_name == name:
                continue
            await self._ensure_session(
                description=f"{name} to {peer_name}",
                device_id=device.id,
                local_ip_id=loopback_ip.id,
                remote_ip_id=peer_ip_id,
                backbone_as_id=backbone_as_id,
            )
            await self._ensure_session(
                description=f"{peer_name} to {name}",
                device_id=peer_id,
                local_ip_id=peer_ip_id,
                remote_ip_id=loopback_ip.id,
                backbone_as_id=backbone_as_id,
            )

    async def _ensure_session(
        self,
        *,
        description: str,
        device_id: str,
        local_ip_id: str,
        remote_ip_id: str,
        backbone_as_id: str,
    ) -> None:
        """Upsert one RoutingBGPSession; its HFID is [device, description], so upsert is idempotent.

        Args:
            description: ``<holder> to <peer>``, the session's natural key next to its device.
            device_id: Infrahub id of the device that holds the session.
            local_ip_id: Infrahub id of the holder's Loopback0 address.
            remote_ip_id: Infrahub id of the peer's Loopback0 address.
            backbone_as_id: Infrahub id of the backbone RoutingAutonomousSystem.
        """
        session = await self.client.create(
            kind="RoutingBGPSession",
            branch=self.branch,
            description=description,
            session_type="INTERNAL",
            role="backbone",
            status="active",
            device={"id": device_id},
            local_as={"id": backbone_as_id},
            remote_as={"id": backbone_as_id},
            local_ip={"id": local_ip_id},
            remote_ip={"id": remote_ip_id},
        )
        await session.save(allow_upsert=True)

    async def _ensure_memberships(self, device: Any, platform: str, backbone_id: str) -> None:
        """Add the device to `pes`, its platform artifact group, and the backbone's `pes`.

        ``add_relationships`` rather than ``.add()`` + ``save()``: a save re-sends
        the whole member list (dropping anything beyond the loaded page) and
        claims the group for this run's tracking group. RelationshipAdd does
        neither, and the server emits the same membership event.

        Args:
            device: The new PE's DcimDevice node.
            platform: Platform name, which selects the artifact target group.
            backbone_id: Infrahub id of the TopologyMplsBackbone.
        """
        for group_name in ("pes", PLATFORM_GROUP[platform]):
            group: Any = await self.client.get(
                kind="CoreStandardGroup", name__value=group_name, branch=self.branch
            )
            await group.add_relationships(relation_to_update="members", related_nodes=[device.id])
        backbone: Any = await self.client.get(
            kind="TopologyMplsBackbone", id=backbone_id, branch=self.branch
        )
        await backbone.add_relationships(relation_to_update="pes", related_nodes=[device.id])
