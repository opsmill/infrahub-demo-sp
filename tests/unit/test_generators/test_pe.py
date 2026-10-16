"""Unit tests for the PE onboarding generator."""

from __future__ import annotations

import ipaddress
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.common import find_or_claim_core_interface
from generators.generate_l3vpn import L3VpnGenerator
from generators.generate_pe import PeOnboardingGenerator

LOOPBACKS = {
    "pe-02": ("d2", "ip2", "10.0.0.2/32"),
    "pe-07": ("d7", "ip7", "10.0.0.7/32"),
}


def _request(
    name: str = "pe-09",
    uplinks: tuple[str, ...] = ("pe-07", "pe-02"),
    platform: str = "arista_eos",
) -> dict:
    """A TopologyPeOnboarding payload shaped like queries/service/pe_onboarding.gql."""
    return {
        "TopologyPeOnboarding": {
            "edges": [
                {
                    "node": {
                        "id": "req-1",
                        "name": {"value": name},
                        "status": {"value": "draft"},
                        "location": {"node": {"id": "loc-mil", "shortname": {"value": "mil"}}},
                        "platform": {"node": {"id": "plat-1", "name": {"value": platform}}},
                        "device_type": {"node": {"id": "dt-1", "name": {"value": "7280R3"}}},
                        "backbone": {
                            "node": {
                                "id": "bb-1",
                                "name": {"value": "mpls-backbone-1"},
                                "isis_area": {"value": "49.0001"},
                                "isis_level": {"value": "level-2"},
                                "asn": {"node": {"id": "as-65000", "asn": {"value": 65000}}},
                            }
                        },
                        "uplinks": {
                            "edges": [
                                {
                                    "node": {
                                        "id": f"d-{u}",
                                        "name": {"value": u},
                                        "role": {"value": "pe"},
                                    }
                                }
                                for u in uplinks
                            ]
                        },
                    }
                }
            ]
        }
    }


def _node(**attrs: Any) -> MagicMock:
    """A saved-node stand-in whose attributes carry ``.value``."""
    node = MagicMock(save=AsyncMock(), add_relationships=AsyncMock())
    node.id = attrs.pop("id", "new-id")
    for key, value in attrs.items():
        getattr(node, key).value = value
    return node


def _make_gen(
    payload: dict,
    *,
    existing_by_kind: dict[str, list[Any]] | None = None,
    linked_device_id: str | None = None,
) -> tuple[PeOnboardingGenerator, MagicMock]:
    """Build a generator with a mocked client.

    ``client.filters`` answers per kind from ``existing_by_kind`` (default: nothing
    exists), ``client.create`` returns a fresh node per call that remembers its
    kwargs, and ``client.get`` returns a node with ``add_relationships``. The
    fetched request links to ``linked_device_id`` (default: no device yet).
    """
    client = MagicMock()
    client.fetched_nodes = []
    client.created_devices = []
    existing = existing_by_kind or {}

    async def _filters(**kwargs: Any) -> list[Any]:
        return list(existing.get(str(kwargs.get("kind")), []))

    async def _create(**kwargs: Any) -> MagicMock:
        node = _node(
            id=f"id-{kwargs.get('kind')}-{kwargs.get('name') or kwargs.get('description')}"
        )
        node.create_kwargs = kwargs
        for key, value in kwargs.items():
            if isinstance(value, (str, int, list)):
                getattr(node, key).value = value
        if kwargs.get("kind") == "DcimDevice":
            client.created_devices.append(node)
        return node

    async def _get(**kwargs: Any) -> MagicMock:
        node = _node(id=f"get-{kwargs.get('kind')}-{kwargs.get('name__value') or kwargs.get('id')}")
        node.get_kwargs = kwargs
        if kwargs.get("kind") == "TopologyPeOnboarding":
            node.device.id = linked_device_id
        client.fetched_nodes.append(node)
        return node

    client.filters = AsyncMock(side_effect=_filters)
    client.create = AsyncMock(side_effect=_create)
    client.get = AsyncMock(side_effect=_get)
    client.allocate_next_ip_address = AsyncMock(
        # The real SDK returns ipaddress objects for IP attributes, not strings.
        return_value=_node(id="ip-9", address=ipaddress.IPv4Interface("10.0.0.9/32"))
    )

    gen = PeOnboardingGenerator.__new__(PeOnboardingGenerator)
    gen.client = client
    gen.data = payload
    gen.branch = "backbone/add-pe-09"
    return gen, client


def _creates(client: MagicMock, kind: str) -> list[dict[str, Any]]:
    return [c.kwargs for c in client.create.await_args_list if c.kwargs.get("kind") == kind]


def _prefix(network: str) -> MagicMock:
    return _node(
        id=f"prefix-{network}", prefix=ipaddress.IPv4Network(network), description="", role=""
    )


@pytest.mark.asyncio
async def test_empty_payload_is_a_no_op() -> None:
    gen, client = _make_gen({"TopologyPeOnboarding": {"edges": []}})
    await gen.generate()
    client.create.assert_not_called()
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fewer_than_two_uplinks_raises() -> None:
    gen, _ = _make_gen(_request(uplinks=("pe-02",)))
    with pytest.raises(RuntimeError, match="at least 2 uplinks"):
        await gen.generate()


@pytest.mark.asyncio
async def test_unknown_platform_raises() -> None:
    gen, _ = _make_gen(_request(platform="vyos"))
    with pytest.raises(RuntimeError, match="No artifact group for platform 'vyos'"):
        await gen.generate()


@pytest.mark.asyncio
async def test_first_run_materialises_everything() -> None:
    gen, client = _make_gen(_request())
    claimed = {
        "pe-02": _node(id="port-02", name="Ethernet5"),
        "pe-07": _node(id="port-07", name="Ethernet5"),
    }
    with (
        patch(
            "generators.generate_pe.allocate_prefix_from_pool",
            AsyncMock(side_effect=[_prefix("10.1.0.30/31"), _prefix("10.1.0.32/31")]),
        ) as alloc,
        patch(
            "generators.generate_pe.find_or_claim_core_interface",
            AsyncMock(side_effect=lambda c, dev, desc, b: claimed[dev]),
        ) as claim,
        patch("generators.generate_pe.backbone_pe_loopbacks", AsyncMock(return_value=LOOPBACKS)),
    ):
        await gen.generate()

    # Device: role pe, backbone AS, request's platform / type / site by id.
    devices = _creates(client, "DcimDevice")
    assert len(devices) == 1
    assert devices[0]["role"] == "pe"
    assert devices[0]["asn"] == {"id": "as-65000"}
    assert devices[0]["location"] == {"id": "loc-mil"}

    # Loopback0 plus a pool allocation keyed by the request.
    loopbacks = _creates(client, "InterfaceVirtual")
    assert [lb["name"] for lb in loopbacks] == ["Loopback0"]
    assert client.allocate_next_ip_address.await_args.kwargs["identifier"] == (
        "pe-onboarding-req-1-loopback"
    )

    # Processes, with the NET derived from the allocated loopback.
    isis = _creates(client, "MplsIsisProcess")
    assert isis[0]["net_id"] == "49.0001.0100.0000.0009.00"
    assert isis[0]["level"] == "level-2"
    assert _creates(client, "MplsLdpProcess")[0]["router_id"] == "10.0.0.9"
    assert _creates(client, "MplsBgpProcess")[0]["address_families"] == ["vpnv4", "vpnv6"]

    # Uplinks in sorted order: Ethernet1 faces pe-02, Ethernet2 faces pe-07.
    ports = _creates(client, "InterfacePhysical")
    core_ports = [port for port in ports if port["role"] == "core"]
    assert [(p["name"], p["description"], p["role"], p["mtu"]) for p in core_ports] == [
        ("Ethernet1", "To pe-02", "core", 9000),
        ("Ethernet2", "To pe-07", "core", 9000),
    ]
    spare_ports = [port for port in ports if port["status"] == "free"]
    assert [(port["name"], port["role"], port["mtu"]) for port in spare_ports] == [
        ("Ethernet3", "cust", 1500),
        ("Ethernet4", "cust", 1500),
    ]
    assert [c.kwargs["identifier"] for c in alloc.await_args_list] == [
        "pe-onboarding-req-1-pe-02",
        "pe-onboarding-req-1-pe-07",
    ]
    assert [c.args[1:3] for c in claim.await_args_list] == [
        ("pe-02", "To pe-09"),
        ("pe-07", "To pe-09"),
    ]
    # Both halves of each /31: new PE gets the even address, the uplink the odd one.
    addresses = {a["address"]: a["interface"] for a in _creates(client, "IpamIPAddress")}
    assert set(addresses) == {"10.1.0.30/31", "10.1.0.31/31", "10.1.0.32/31", "10.1.0.33/31"}
    assert addresses["10.1.0.31/31"] is claimed["pe-02"]
    assert addresses["10.1.0.33/31"] is claimed["pe-07"]

    # iBGP mesh in both directions to every other PE.
    sessions = _creates(client, "RoutingBGPSession")
    assert {s["description"] for s in sessions} == {
        "pe-09 to pe-02",
        "pe-02 to pe-09",
        "pe-09 to pe-07",
        "pe-07 to pe-09",
    }
    assert all(s["session_type"] == "INTERNAL" and s["role"] == "backbone" for s in sessions)
    reverse = next(s for s in sessions if s["description"] == "pe-02 to pe-09")
    assert reverse["device"] == {"id": "d2"}
    assert reverse["local_ip"] == {"id": "ip2"}
    assert reverse["remote_ip"] == {"id": "ip-9"}

    # Memberships go through RelationshipAdd on the fetched group / backbone,
    # never a tracked save that would claim them for this run.
    fetched = {
        (n.get_kwargs.get("kind"), n.get_kwargs.get("name__value") or n.get_kwargs.get("id")): n
        for n in client.fetched_nodes
    }
    device_id = devices_created[0].id if (devices_created := client.created_devices) else None
    for key in (
        ("CoreStandardGroup", "pes"),
        ("CoreStandardGroup", "pe_arista_eos"),
    ):
        fetched[key].add_relationships.assert_awaited_once_with(
            relation_to_update="members", related_nodes=[device_id]
        )
        fetched[key].save.assert_not_awaited()
    fetched[("TopologyMplsBackbone", "bb-1")].add_relationships.assert_awaited_once_with(
        relation_to_update="pes", related_nodes=[device_id]
    )

    # The request is linked to the device as soon as it exists, then promoted
    # to active at the end: two saves.
    request_node = fetched[("TopologyPeOnboarding", "req-1")]
    assert request_node.device is client.created_devices[0]
    assert request_node.status.value == "active"
    assert request_node.save.await_count == 2
    request_node.save.assert_awaited_with(allow_upsert=True)


@pytest.mark.asyncio
async def test_second_run_adopts_existing_nodes_and_resaves_them() -> None:
    """A re-run must re-save every owned node (the reaper rule) and create no duplicates."""
    device = _node(id="dev-9", name="pe-09")
    loopback_iface = _node(id="lb-if", name="Loopback0")
    existing_ip = _node(id="ip-x", address="10.1.0.30/31")
    gen, client = _make_gen(
        _request(),
        existing_by_kind={
            "DcimDevice": [device],
            "InterfaceVirtual": [loopback_iface],
            "IpamIPAddress": [existing_ip],
        },
        linked_device_id="dev-9",
    )
    with (
        patch(
            "generators.generate_pe.allocate_prefix_from_pool",
            AsyncMock(side_effect=[_prefix("10.1.0.30/31"), _prefix("10.1.0.32/31")]),
        ),
        patch(
            "generators.generate_pe.find_or_claim_core_interface",
            AsyncMock(return_value=_node(id="port", name="Ethernet5")),
        ),
        patch("generators.generate_pe.backbone_pe_loopbacks", AsyncMock(return_value=LOOPBACKS)),
    ):
        await gen.generate()

    assert _creates(client, "DcimDevice") == []
    assert _creates(client, "InterfaceVirtual") == []
    assert _creates(client, "IpamIPAddress") == []
    device.save.assert_awaited()
    loopback_iface.save.assert_awaited()
    # Four address lookups all hit the same existing row; every adoption re-saves it.
    assert existing_ip.save.await_count == 4
    # Sessions are upserts keyed by [device, description], so they are always re-created.
    assert len(_creates(client, "RoutingBGPSession")) == 4


@pytest.mark.asyncio
async def test_existing_device_not_linked_to_the_request_is_not_adopted() -> None:
    """Adopting a device the request did not create would let the reaper delete it later."""
    device = _node(id="dev-other", name="pe-09")
    gen, client = _make_gen(_request(), existing_by_kind={"DcimDevice": [device]})
    with pytest.raises(RuntimeError, match="already exists and was not created by this request"):
        await gen.generate()
    device.save.assert_not_awaited()
    client.create.assert_not_called()


@pytest.mark.asyncio
async def test_ibgp_mesh_reads_only_the_request_backbone() -> None:
    gen, _ = _make_gen(_request())
    with (
        patch(
            "generators.generate_pe.allocate_prefix_from_pool",
            AsyncMock(side_effect=[_prefix("10.1.0.30/31"), _prefix("10.1.0.32/31")]),
        ),
        patch(
            "generators.generate_pe.find_or_claim_core_interface",
            AsyncMock(return_value=_node(id="port", name="Ethernet5")),
        ),
        patch(
            "generators.generate_pe.backbone_pe_loopbacks", AsyncMock(return_value=LOOPBACKS)
        ) as loopbacks,
    ):
        await gen.generate()
    loopbacks.assert_awaited_once_with(gen.client, gen.branch, "bb-1")


@pytest.mark.asyncio
async def test_spare_ports_support_services_and_uplinks_and_keep_claims_on_rerun() -> None:
    """Consume the new ports through both allocators, then rerun without freeing them."""
    gen, client = _make_gen(_request())
    device = _node(id="dev-9", name="pe-09")
    ports: dict[str, MagicMock] = {}

    async def stored_ports(**kwargs: Any) -> list[MagicMock]:
        return [
            port
            for port in ports.values()
            if all(
                getattr(port, key.removesuffix("__value")).value == value
                for key, value in kwargs.items()
                if key in {"name__value", "description__value", "status__value"}
            )
        ]

    async def create_port(**kwargs: Any) -> MagicMock:
        port = _node(
            **{key: kwargs[key] for key in ("name", "description", "status", "role", "mtu")}
        )
        ports[kwargs["name"]] = port
        return port

    client.filters = AsyncMock(side_effect=stored_ports)
    client.create = AsyncMock(side_effect=create_port)
    await gen._ensure_spare_interfaces(device, uplink_count=2)

    service = L3VpnGenerator.__new__(L3VpnGenerator)
    service.client, service.branch = client, gen.branch
    service._claimed_pe_ports = set()
    customer_port = await service._ensure_pe_interface(
        {"pe_device": {"node": {"name": {"value": "pe-09"}}}},
        {"name": {"value": "new-vpn"}},
    )
    core_port = await find_or_claim_core_interface(client, "pe-09", "To pe-10", gen.branch)
    for port in ports.values():
        port.save.reset_mock()
    await gen._ensure_spare_interfaces(device, uplink_count=2)

    assert customer_port is ports["Ethernet3"]
    assert customer_port.status.value == "active" and customer_port.role.value == "cust"
    assert customer_port.description.value == "L3VPN new-vpn"
    assert core_port is ports["Ethernet4"]
    assert core_port.status.value == "active" and core_port.role.value == "core"
    assert core_port.description.value == "To pe-10" and core_port.mtu.value == 9000
    assert client.create.await_count == 2
    for port in ports.values():
        port.save.assert_awaited_once_with(allow_upsert=True)
