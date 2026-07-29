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
        )
