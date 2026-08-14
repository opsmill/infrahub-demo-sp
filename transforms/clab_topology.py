"""Containerlab topology transform for the MPLS backbone."""

from __future__ import annotations

import ipaddress
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, StrictUndefined

LOG = logging.getLogger(__name__)

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


def _lan_host_address(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> Any | None:
    """Return the customer host's address inside ``network``, or ``None``.

    :data:`LAN_HOST_OFFSET` is the preferred offset, but adding it blindly walks
    straight out of any prefix shorter than a /28: a /30 gateway of ``x.x.x.1``
    yielded a host at ``x.x.x.10``, in a different network, so ``ip route replace
    default via <gateway>`` failed with "Network is unreachable" and the host came
    up silently useless. Fall back to the last usable address when the offset does
    not fit, and give up when the prefix has no room for a host beside the
    gateway.

    Args:
        network: The customer LAN the gateway sits in.

    Returns:
        An address strictly inside ``network`` and clear of the ``.1`` gateway, or
        ``None`` when the prefix is too small to hold one.
    """
    # .0 network, .1 gateway, and (IPv4) a broadcast address at the top.
    usable = network.num_addresses - 3 if network.version == 4 else network.num_addresses - 2
    if usable < 1:
        return None
    offset = LAN_HOST_OFFSET if LAN_HOST_OFFSET <= usable + 1 else usable + 1
    return network.network_address + offset


def _first_ipv4(addr_edges: list[dict[str, Any]]) -> str | None:
    """Return the first IPv4 address among a port's addresses, or ``None``.

    Args:
        addr_edges: The ``ip_addresses`` edges of one interface.

    Returns:
        The address in CIDR form, or ``None`` when the port carries no IPv4.
    """
    for edge in addr_edges:
        value = edge["node"]["address"]["value"]
        try:
            if ipaddress.ip_interface(value).version == 4:
                return str(value)
        except ValueError:
            continue
    return None


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
            # Pick the IPv4 address explicitly rather than trusting edge order.
            # `clab.gql` does not filter by family, and GraphQL edge order is not
            # contractual: on a dual-stack core port whose v6 edge happened to
            # come first, this port would group under its /127 while its peer
            # grouped under the /31, both networks would hold a single endpoint,
            # and the `len(endpoints) != 2` filter below would drop the link —
            # partitioning the lab with nothing logged.
            address = _first_ipv4(addr_edges)
            if address is None:
                LOG.warning(
                    "Core port %s:%s carries no IPv4 address; excluded from the backbone",
                    pe_name,
                    iface["name"]["value"],
                )
                continue
            if len(addr_edges) > 1:
                LOG.warning(
                    "Core port %s:%s carries %d addresses; pairing on %s",
                    pe_name,
                    iface["name"]["value"],
                    len(addr_edges),
                    address,
                )
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


def _ce_attachments(data: dict[str, Any], backbone: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the lab-deployable PE-CE attachments, one per L3VPN site.

    A site contributes a CE node and a PE-CE link only when everything the lab
    needs is present: a CE device on a lab-deployable platform, the CE port
    facing the PE, and the PE port allocated by the L3VPN generator. Sites whose
    generator hasn't run yet, or whose CE is unmanaged, are skipped rather than
    rendered as a half-wired node.

    The site's PE must also belong to the backbone being rendered. The query
    returns every ``ServiceL3VpnSite`` in the database, unfiltered, so a site
    hanging off some other backbone's PE would otherwise emit a link endpoint
    naming a node this topology never declares — which containerlab rejects
    outright, taking the whole lab down rather than just that site.

    Args:
        data: Result of the ``clab_topology`` GraphQL query.
        backbone: The ``TopologyMplsBackbone`` node being rendered.

    Returns:
        A deterministically ordered list of attachments, each a dict with
        ``ce`` and ``pe`` ``{device, iface, kind}`` mappings.
    """
    backbone_pes = {pe["name"]["value"] for pe in _labbed_pes(backbone)}
    attachments: list[dict[str, Any]] = []
    # Two sites naming the same pair of ports describe one cable, and clab rejects
    # the same endpoint appearing in two links.
    seen_links: set[tuple[str, str, str, str]] = set()
    # Each physical port is one end of one cable, so an endpoint may be claimed
    # once even when the full four-tuple differs. It does differ: the shipped CEs
    # have a single upstream port, so a second VPN's site on the same CE reuses
    # it, while `_ensure_pe_interface` keys per VPN and hands that site a
    # different PE port. Both links were emitted, `ce-…:eth1` appeared twice, and
    # containerlab rejected the entire topology rather than the one link. Same
    # guard as `wired_ports` in _lan_hosts.
    claimed_ports: set[tuple[str, str]] = set()
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
        if pe_device["name"]["value"] not in backbone_pes:
            continue
        link_key = (
            pe_device["name"]["value"],
            pe_iface["name"]["value"],
            ce_device["name"]["value"],
            ce_iface["name"]["value"],
        )
        if link_key in seen_links:
            continue
        ce_port = (ce_device["name"]["value"], ce_iface["name"]["value"])
        pe_port = (pe_device["name"]["value"], pe_iface["name"]["value"])
        if ce_port in claimed_ports or pe_port in claimed_ports:
            LOG.warning(
                "Skipping PE-CE link %s:%s <-> %s:%s: an endpoint is already cabled",
                *pe_port,
                *ce_port,
            )
            continue
        seen_links.add(link_key)
        claimed_ports.update((ce_port, pe_port))
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


def _lan_hosts(data: dict[str, Any], attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

    A host is emitted only for a CE that ``attachments`` actually declares as a
    node. The gate here used to be weaker than the one in
    :func:`_ce_attachments`, so a site whose ``pe_interface`` had not been
    allocated yet — the state the generator leaves behind if it fails between
    creating the sub-interface and saving the site — produced a link to a CE that
    was never declared, and containerlab refused to deploy the entire lab.

    Args:
        data: Result of the ``clab_topology`` GraphQL query.
        attachments: The PE-CE attachments from :func:`_ce_attachments`, i.e. the
            CEs this topology declares.

    Returns:
        A deterministically ordered list of hosts, each a dict with ``name``,
        ``vlan``, ``address``, ``gateway``, and the ``ce`` endpoint to wire to.
    """
    declared_ces = {a["ce"]["device"] for a in attachments}

    # (device name, parent port) -> the tagged sub-interface on it. Keying by
    # device alone let a CE with two tagged sub-interfaces keep only the last, so
    # a site could be handed another site's VLAN and gateway.
    tagged: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in data.get("InterfaceVirtual", {}).get("edges", []):
        node = edge["node"]
        vlan = (node.get("dot1q_id") or {}).get("value")
        addresses = [
            a["node"]["address"]["value"] for a in (node.get("ip_addresses") or {}).get("edges", [])
        ]
        parent = (node.get("parent_interface") or {}).get("node")
        # `is None`, not truthiness: `dot1q_id` has no schema minimum, so VLAN 0
        # is representable, and treating it as absent would silently drop the
        # customer host — leaving the CE LAN port with no carrier and no
        # diagnostic. The repo's convention for exactly this is the `peer_asn`
        # macro's `is not none`.
        if vlan is None or not addresses or not parent:
            continue
        key = (node["device"]["node"]["name"]["value"], parent["name"]["value"])
        tagged[key] = {"vlan": vlan, "gateway": addresses[0]}

    hosts: list[dict[str, Any]] = []
    used_names: set[str] = set()
    # One CE LAN port carries one cable, so it gets exactly one host — even when
    # two sites name it. Renaming the second host was not enough: both entries
    # still wired to the same `<ce>:<port>` endpoint, and containerlab rejects an
    # endpoint that appears in two links, taking the whole lab down. Same guard
    # as `seen_links` in _ce_attachments.
    wired_ports: set[tuple[str, str]] = set()
    for edge in data.get("ServiceL3VpnSite", {}).get("edges", []):
        site = edge["node"]
        ce_device = (site.get("ce_device") or {}).get("node")
        private = (site.get("ce_private_interface") or {}).get("node")
        if not ce_device or not private:
            continue
        ce_name = ce_device["name"]["value"]
        if _pe_kind(ce_device) not in LABBED_KINDS or ce_name not in declared_ces:
            continue
        private_name = private["name"]["value"]
        if (ce_name, private_name) in wired_ports:
            continue
        sub = tagged.get((ce_name, private_name))
        if sub is None:
            continue
        gateway = sub["gateway"]
        network = ipaddress.ip_interface(gateway).network
        host_ip = _lan_host_address(network)
        if host_ip is None:
            continue
        # A CE terminating two sites on *different* LAN ports needs two hosts, so
        # the name has to be disambiguated by port rather than by CE alone.
        name = _lan_host_name(ce_name)
        if name in used_names:
            name = f"{name}-{private_name.replace('/', '-').lower()}"
            if name in used_names:
                continue
        used_names.add(name)
        wired_ports.add((ce_name, private_name))
        hosts.append(
            {
                "name": name,
                "vlan": sub["vlan"],
                "address": f"{host_ip}/{network.prefixlen}",
                "gateway": str(ipaddress.ip_interface(gateway).ip),
                "ce": {
                    "device": ce_name,
                    "iface": private_name,
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
        ce_attachments = _ce_attachments(data, backbone)
        env = Environment(
            loader=FileSystemLoader(TEMPLATES_DIR),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )
        template = env.get_template("clab_topology.j2")
        return template.render(
            data=data,
            labbed_pes=_labbed_pes(backbone),
            backbone_links=_backbone_links(backbone),
            ce_attachments=ce_attachments,
            lan_hosts=_lan_hosts(data, ce_attachments),
        )
