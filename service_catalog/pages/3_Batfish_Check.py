"""Batfish Check page — run pybatfish queries against rendered MPLS backbone configs.

Mirrors what `uv run invoke batfish` does on the host, but executes the same
query battery inside the Streamlit container against the same batfish sidecar.
Findings are bucketed by severity (error / warning / info) and rendered with
per-stage progress so a long Batfish init isn't a blank-screen wait.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

import streamlit as st
from infrahub_sdk.exceptions import NodeNotFoundError
from utils import client_for, run_async

from checks.batfish_helpers import (
    SUPPORTED_PLATFORMS,
    Finding,
    findings_from_bgp_session_compat,
    findings_from_isis_edges,
    findings_from_parse_status,
    findings_from_parse_warning,
    findings_from_undefined_references,
    wait_for_batfish,
)

# Per-platform artifact name. Kept in sync with the same mapping in
# `checks/batfish_backbone.py` — both files reach into Infrahub's
# CoreArtifact rows by name to fetch the rendered config.
_ARTIFACT_NAME_BY_PLATFORM = {
    "arista_eos": "pe-arista-eos",
    "cisco_iosxr": "pe-cisco-iosxr",
    "juniper_junos": "pe-juniper-junos",
}

# Same query the registered `BatfishBackboneCheck` runs. Used directly here
# so we get the platform.name nested field in one round trip without having
# to chase relationships through the SDK store.
_BACKBONE_QUERY = """
query BatfishBackbone($name: String!) {
  TopologyMplsBackbone(name__value: $name) {
    edges {
      node {
        name { value }
        pes {
          edges {
            node {
              id
              name { value }
              platform {
                node {
                  name { value }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _branches() -> list[str]:
    """Return the list of branch names the Streamlit container can see."""
    return list(run_async(client_for().branch.all()).keys())


def _backbones(branch: str) -> list[str]:
    """Return MPLS backbone names on ``branch``.

    Surfaces the real names from Infrahub so users can't typo a non-existent
    backbone — the check would otherwise just iterate zero rows and report
    a misleading PASS.
    """
    client = client_for(branch=branch)
    nodes = run_async(client.all(kind="TopologyMplsBackbone"))
    return sorted(n.name.value for n in nodes)


def _fetch_artifact_body(client: Any, pe_id: str, platform_name: str) -> str | None:
    """Return the latest rendered config body for ``pe_id``, or None if missing.

    Mirrors ``BatfishBackboneCheck._fetch_artifact`` — kept inline here so the
    page can drive its own progress UI without instantiating the check class.
    """
    artifact_def = _ARTIFACT_NAME_BY_PLATFORM[platform_name]
    try:
        artifact = run_async(
            client.get(kind="CoreArtifact", object__ids=[pe_id], name__value=artifact_def)
        )
    except NodeNotFoundError:
        return None
    storage_id_attr = getattr(artifact, "storage_id", None)
    storage_id = storage_id_attr.value if storage_id_attr is not None else None
    if not storage_id:
        return None
    return run_async(client.object_store.get(identifier=storage_id))


def _run_queries_with_progress(
    session: Any, expected_hosts: set[str], status: Any
) -> list[Finding]:
    """Execute the 5 pybatfish queries one-by-one so progress is visible.

    ``run_snapshot()`` in the helpers calls them in a single shot — that's the
    right shape for a non-interactive check, but the page wants to surface
    each query as it runs.
    """
    findings: list[Finding] = []
    plan = [
        ("Parse status", "fileParseStatus", findings_from_parse_status),
        ("Parse warnings", "parseWarning", findings_from_parse_warning),
        ("Undefined references", "undefinedReferences", findings_from_undefined_references),
        ("BGP session compatibility", "bgpSessionCompatibility", findings_from_bgp_session_compat),
    ]
    for label, attr, mapper in plan:
        status.write(f"⏳ Running query: **{label}**")
        df = getattr(session.q, attr)().answer().frame()
        findings.extend(mapper(df))
        status.write(f"✓ {label}: {len(df)} rows answered")

    status.write("⏳ Running query: **IS-IS edges**")
    isis_df = session.q.isisEdges().answer().frame()
    findings.extend(findings_from_isis_edges(isis_df, expected_hosts=expected_hosts))
    status.write(f"✓ IS-IS edges: {len(isis_df)} rows answered")
    return findings


def _render_findings(findings: list[Finding]) -> None:
    """Render findings grouped by severity, with counts and per-row detail."""
    by_sev: dict[str, list[Finding]] = {"error": [], "warning": [], "info": []}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)

    cols = st.columns(3)
    cols[0].metric("Errors", len(by_sev["error"]), delta_color="inverse")
    cols[1].metric("Warnings", len(by_sev["warning"]), delta_color="off")
    cols[2].metric("Info", len(by_sev["info"]))

    if not findings:
        st.success("Clean run — no findings.")
        return

    tabs = st.tabs(
        [
            f"🚫 Errors ({len(by_sev['error'])})",
            f"⚠️ Warnings ({len(by_sev['warning'])})",
            f"ℹ️ Info ({len(by_sev['info'])})",
        ]
    )
    for tab, sev in zip(tabs, ("error", "warning", "info"), strict=True):
        with tab:
            rows = by_sev[sev]
            if not rows:
                st.caption(f"No {sev} findings.")
                continue
            # Group by query for scanability.
            by_query: dict[str, list[Finding]] = {}
            for f in rows:
                by_query.setdefault(f.query, []).append(f)
            for query, items in sorted(by_query.items()):
                with st.expander(f"`{query}` — {len(items)} finding(s)", expanded=True):
                    for f in items:
                        node_label = f"`{f.node}`" if f.node else "—"
                        st.markdown(f"- {node_label}: {f.message}")


st.set_page_config(page_title="Batfish Check", page_icon="🧪", layout="wide")
st.title("🧪 Batfish Check")
st.caption(
    "Static-analyze the rendered MPLS backbone configs with the Batfish sidecar. "
    "Same query battery the `BatfishBackboneCheck` runs on a proposed change."
)

col_branch, col_backbone = st.columns([1, 1])
with col_branch:
    branch = st.selectbox("Branch", options=_branches(), index=0)
with col_backbone:
    backbones = _backbones(branch)
    if not backbones:
        st.error(f"No `TopologyMplsBackbone` nodes on branch `{branch}` — nothing to check.")
        st.stop()
    backbone_name = st.selectbox("Backbone", options=backbones, index=0)

run = st.button("Run check", type="primary", use_container_width=False)

if run:
    client = client_for(branch=branch)

    # Stage 1: pull the backbone + its PEs.
    with st.status("Fetching backbone topology …", expanded=False) as status:
        result = run_async(
            client.execute_graphql(query=_BACKBONE_QUERY, variables={"name": backbone_name})
        )
        edges = result.get("TopologyMplsBackbone", {}).get("edges", [])
        if not edges:
            status.update(label=f"No backbone named {backbone_name!r}", state="error")
            st.stop()

        pe_rows: list[tuple[str, str, str]] = []  # (id, name, platform_name)
        skipped: list[tuple[str, str]] = []  # (name, platform_name)
        for pe_edge in edges[0]["node"].get("pes", {}).get("edges", []):
            pe = pe_edge["node"]
            platform_node = (pe.get("platform") or {}).get("node") or {}
            platform_name = (platform_node.get("name") or {}).get("value") or ""
            pe_name = pe["name"]["value"]
            if platform_name in SUPPORTED_PLATFORMS:
                pe_rows.append((pe["id"], pe_name, platform_name))
            else:
                skipped.append((pe_name, platform_name))
        status.update(
            label=(
                f"Backbone {backbone_name!r}: "
                f"{len(pe_rows)} supported PE(s), {len(skipped)} skipped"
            ),
            state="complete",
        )

    if skipped:
        with st.expander(f"{len(skipped)} PE(s) skipped (Batfish doesn't parse)", expanded=False):
            for name, plat in skipped:
                st.markdown(f"- `{name}`  *(platform `{plat}`)*")

    if not pe_rows:
        st.warning("No supported PEs in this backbone. Nothing for Batfish to validate.")
        st.stop()

    # Stage 2: fetch artifacts.
    tmp = Path(tempfile.mkdtemp(prefix=f"batfish-{backbone_name}-"))
    configs_dir = tmp / "configs"
    configs_dir.mkdir()
    hosts_in_snapshot: set[str] = set()
    with st.status(f"Fetching {len(pe_rows)} rendered config(s) …", expanded=True) as status:
        for pe_id, pe_name, platform_name in pe_rows:
            body = _fetch_artifact_body(client, pe_id, platform_name)
            if body is None:
                status.write(f"⚠️ {pe_name}: no rendered artifact yet — skipping")
                continue
            (configs_dir / f"{pe_name}.cfg").write_text(body)
            hosts_in_snapshot.add(pe_name)
            status.write(f"✓ {pe_name} ({len(body):,} bytes)")
        status.update(
            label=f"Loaded {len(hosts_in_snapshot)} config(s) into snapshot dir",
            state="complete",
        )

    if not hosts_in_snapshot:
        st.error("No rendered artifacts available — rebuild artifacts and retry.")
        shutil.rmtree(tmp, ignore_errors=True)
        st.stop()

    host = os.environ.get("BATFISH_HOST", "batfish")
    port = int(os.environ.get("BATFISH_PORT", "9996"))

    # Stage 3: wait for batfish coordinator to respond.
    with st.status(f"Waiting for Batfish coordinator at {host}:{port} …") as status:
        if not wait_for_batfish(host, port=port, timeout_s=60, backoff_s=2):
            status.update(label=f"Batfish unreachable at {host}:{port}", state="error")
            shutil.rmtree(tmp, ignore_errors=True)
            st.stop()
        status.update(label=f"Batfish reachable at {host}:{port}", state="complete")

    # Stage 4: init snapshot — slowest single step (~10–30s).
    # Deferred import: pybatfish is a heavy dep, only loaded once a run starts.
    from pybatfish.client.session import Session  # noqa: PLC0415

    snapshot_name = f"{backbone_name}-{uuid.uuid4().hex[:8]}"
    network = "infrahub-mpls"
    session = Session(host=host)
    findings: list[Finding] = []
    with st.status(f"Initializing snapshot `{snapshot_name}` …", expanded=True) as status:
        session.set_network(network)
        session.init_snapshot(str(tmp), name=snapshot_name, overwrite=True)
        status.write("✓ Snapshot loaded")

        # Stage 5: run the queries one by one.
        findings = _run_queries_with_progress(session, hosts_in_snapshot, status)

        try:
            session.delete_snapshot(snapshot_name)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            status.write(f"(snapshot cleanup warning: {exc})")
        status.update(label=f"Completed — {len(findings)} finding(s)", state="complete")

    shutil.rmtree(tmp, ignore_errors=True)

    st.markdown("---")
    st.subheader("Results")
    _render_findings(findings)
