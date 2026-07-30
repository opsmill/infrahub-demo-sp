"""Containerlab topology transform for the MPLS backbone."""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from pathlib import Path
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# containerlab_os values that map to a lab-deployable container image.
LABBED_KINDS = frozenset({"ceos", "srl"})

# Host offset within the customer LAN for the simulated customer machine. The
# gateway is .1 (on the CE sub-interface), so .10 is safely clear of it.
LAN_HOST_OFFSET = 10


def _lan_host_name(ce_name: str) -> str:
    """Return the container name for the machine on a CE's LAN.

    Named `cust-<site>` rather than `host-<ce name>`: the latter embeds the CE's
    own name, so `host-ce-ib-zrh` reads as if it were `ce-ib-zrh` itself and gets
    mistaken for the router. `cust-ib-zrh` cannot be confused with it.

    Args:
        ce_name: Name of the CE router, e.g. ``ce-ib-zrh``.

    Returns:
        The host container name, e.g. ``cust-ib-zrh``.
    """
    suffix = ce_name[len("ce-") :] if ce_name.startswith("ce-") else ce_name
    return f"cust-{suffix}"


def _pe_kind(pe: dict[str, Any]) -> str | None:
    """Return a PE's containerlab kind, or ``None`` if it has no platform.

    Args:
        pe: A ``DcimDevice`` node from the query result.

    Returns:
        The ``containerlab_os`` value, or ``None`` when the platform (or its
        ``containerlab_os``) is unset — such a PE has no lab image and is
        skipped rather than aborting the whole render.
    """
    platform = (pe.get("platform") or {}).get("node")
    if not platform:
        return None
    return (platform.get("containerlab_os") or {}).get("value")


def _labbed_pes(backbone: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the backbone PEs that map to a lab-deployable container image.

    This is the single source of truth for "which PEs are lab-deployable";
    both the node list and the backbone-link derivation consume it, and the
    template renders the returned nodes directly.

    Args:
        backbone: The ``TopologyMplsBackbone`` node from the query result.

    Returns:
        The PE nodes, in query order, whose ``containerlab_os`` is in
        :data:`LABBED_KINDS`.
    """
    return [
        edge["node"]
        for edge in backbone.get("pes", {}).get("edges", [])
        if _pe_kind(edge["node"]) in LABBED_KINDS
    ]


def _backbone_links(backbone: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive lab backbone links from the PEs' core interface addressing.

    The schema has no explicit interface-to-interface peering relationship;
    backbone p2p links are expressed implicitly by two ``core`` interfaces
    sharing the same /31 (or /30) prefix. This groups the core interfaces of
    lab-deployable PEs by the network of their IP address and emits one link
    per network that has exactly two lab-deployable endpoints. Links between a
    lab-deployable PE and a non-deployable one (e.g. Cisco/Juniper in the ISP
    dataset, which have no clab image) are skipped because only one endpoint
    is present.

    Args:
        backbone: The ``TopologyMplsBackbone`` node from the query result.

    Returns:
        A deterministically ordered list of links, each a dict with an
        ``endpoints`` list of ``{device, iface, kind}`` mappings.
    """
    by_network: dict[str, list[dict[str, str]]] = defaultdict(list)
    for pe in _labbed_pes(backbone):
        kind = _pe_kind(pe)
        if kind is None:  # unreachable: _labbed_pes only yields labbed PEs
            continue
        pe_name = pe["name"]["value"]
        for if_edge in pe.get("interfaces", {}).get("edges", []):
            iface = if_edge["node"]
            if iface.get("role", {}).get("value") != "core":
                continue
            addr_edges = iface.get("ip_addresses", {}).get("edges", [])
            if not addr_edges:
                continue
            address = addr_edges[0]["node"]["address"]["value"]
            network = str(ipaddress.ip_interface(address).network)
            by_network[network].append(
                {"device": pe_name, "iface": iface["name"]["value"], "kind": kind}
            )

    links: list[dict[str, Any]] = []
    for network in sorted(by_network):
        endpoints = by_network[network]
        if len(endpoints) != 2:
            continue
        endpoints.sort(key=lambda e: (e["device"], e["iface"]))
        links.append({"network": network, "endpoints": endpoints})
    return links


def _ce_attachments(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the lab-deployable PE-CE attachments, one per L3VPN site.

    A site contributes a CE node and a PE-CE link only when everything the lab
    needs is present: a CE device on a lab-deployable platform, the CE port
    facing the PE, and the PE port allocated by the L3VPN generator. Sites whose
    generator hasn't run yet, or whose CE is unmanaged, are skipped rather than
    rendered as a half-wired node.

    Args:
        data: Result of the ``clab_topology`` GraphQL query.

    Returns:
        A deterministically ordered list of attachments, each a dict with
        ``ce`` and ``pe`` ``{device, iface, kind}`` mappings.
    """
    attachments: list[dict[str, Any]] = []
    for edge in data.get("ServiceL3VpnSite", {}).get("edges", []):
        site = edge["node"]
        ce_device = (site.get("ce_device") or {}).get("node")
        ce_iface = (site.get("ce_interface") or {}).get("node")
        pe_device = (site.get("pe_device") or {}).get("node")
        pe_iface = (site.get("pe_interface") or {}).get("node")
        if not (ce_device and ce_iface and pe_device and pe_iface):
            continue
        ce_kind, pe_kind = _pe_kind(ce_device), _pe_kind(pe_device)
        if ce_kind not in LABBED_KINDS or pe_kind not in LABBED_KINDS:
            continue
        attachments.append(
            {
                "ce": {
                    "device": ce_device["name"]["value"],
                    "iface": ce_iface["name"]["value"],
                    "kind": ce_kind,
                },
                "pe": {
                    "device": pe_device["name"]["value"],
                    "iface": pe_iface["name"]["value"],
                    "kind": pe_kind,
                },
            }
        )
    attachments.sort(key=lambda a: (a["ce"]["device"], a["ce"]["iface"]))
    return attachments


def _lan_hosts(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one simulated customer host per CE private (LAN) side.

    Without something attached, a CE's LAN port has no carrier: the port stays
    down, the dot1q sub-interface hanging off it sits ``lowerlayerdown``, and the
    customer prefix is never advertised because a down interface contributes no
    connected route for the BGP ``network`` statement to match. Attaching a host
    gives the port carrier and makes the VLAN carry real traffic.

    The host is lab scaffolding, not managed infrastructure, so it is synthesised
    here rather than modelled in Infrahub — the same treatment the CE stand-ins
    had before they became real cEOS routers.

    It tags its own frames with the same VLAN the CE expects, so the dot1q
    encapsulation is genuinely exercised rather than merely configured.

    Args:
        data: Result of the ``clab_topology`` GraphQL query.

    Returns:
        A deterministically ordered list of hosts, each a dict with ``name``,
        ``vlan``, ``address``, ``gateway``, and the ``ce`` endpoint to wire to.
    """
    # device name -> the tagged sub-interface on it (VLAN + gateway address)
    tagged: dict[str, dict[str, Any]] = {}
    for edge in data.get("InterfaceVirtual", {}).get("edges", []):
        node = edge["node"]
        vlan = (node.get("dot1q_id") or {}).get("value")
        addresses = [
            a["node"]["address"]["value"] for a in (node.get("ip_addresses") or {}).get("edges", [])
        ]
        if not vlan or not addresses:
            continue
        tagged[node["device"]["node"]["name"]["value"]] = {
            "vlan": vlan,
            "gateway": addresses[0],
        }

    hosts: list[dict[str, Any]] = []
    for edge in data.get("ServiceL3VpnSite", {}).get("edges", []):
        site = edge["node"]
        ce_device = (site.get("ce_device") or {}).get("node")
        private = (site.get("ce_private_interface") or {}).get("node")
        if not ce_device or not private:
            continue
        ce_name = ce_device["name"]["value"]
        if _pe_kind(ce_device) not in LABBED_KINDS or ce_name not in tagged:
            continue
        gateway = tagged[ce_name]["gateway"]
        network = ipaddress.ip_interface(gateway).network
        host_ip = network.network_address + LAN_HOST_OFFSET
        hosts.append(
            {
                "name": _lan_host_name(ce_name),
                "vlan": tagged[ce_name]["vlan"],
                "address": f"{host_ip}/{network.prefixlen}",
                "gateway": str(ipaddress.ip_interface(gateway).ip),
                "ce": {
                    "device": ce_name,
                    "iface": private["name"]["value"],
                    "kind": _pe_kind(ce_device),
                },
            }
        )
    hosts.sort(key=lambda h: h["name"])
    return hosts


class ClabTopology(InfrahubTransform):
    """Render a containerlab YAML topology for the lab-deployable subset of PEs."""

    query = "clab_topology"

    async def transform(self, data: dict[str, Any]) -> str:
        """Render the clab topology template.

        Args:
            data: Result of the ``clab_topology`` GraphQL query.

        Returns:
            Rendered containerlab YAML as plain text.
        """
        backbone = data["TopologyMplsBackbone"]["edges"][0]["node"]
        env = Environment(
            loader=FileSystemLoader(TEMPLATES_DIR),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("clab_topology.j2")
        return template.render(
            data=data,
            labbed_pes=_labbed_pes(backbone),
            backbone_links=_backbone_links(backbone),
            ce_attachments=_ce_attachments(data),
            lan_hosts=_lan_hosts(data),
        )
