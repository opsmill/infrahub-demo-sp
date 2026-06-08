"""Helpers for the Batfish backbone check.

Owns the pybatfish coupling, snapshot lifecycle, query wrappers, and the
internal ``Finding`` dataclass. The check class in ``batfish_backbone`` uses
these helpers and maps ``Finding`` instances to Infrahub log entries.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

import requests

# ``Finding`` and ``SUPPORTED_PLATFORMS`` live in the dependency-free
# ``batfish_common`` module so the task-worker check can use them without
# importing this engine (and its ``requests``/``pandas`` coupling). They are
# re-exported here for the runner and the existing test imports.
from .batfish_common import SUPPORTED_PLATFORMS, Finding

__all__ = [
    "SUPPORTED_PLATFORMS",
    "Finding",
    "findings_from_bgp_session_compat",
    "findings_from_isis_edges",
    "findings_from_parse_status",
    "findings_from_parse_warning",
    "findings_from_undefined_references",
    "run_snapshot",
    "wait_for_batfish",
]

# ``pandas`` is used only in function-signature type annotations; with
# ``from __future__ import annotations`` they're strings, never evaluated at
# import time. Keeping the import guarded keeps module load cheap and
# side-effect-free. At runtime pandas is supplied via the ``pybatfish``
# Session that calls these helpers.
if TYPE_CHECKING:
    import pandas as pd


_PARSE_OK = "PASSED"


def _node_from_row(row: pd.Series[Any], file_name: str) -> str:
    """Pull the first node from a parse-status row, or fall back to the filename stem.

    Args:
        row: A single row from the fileParseStatus DataFrame.
        file_name: The ``File_Name`` value for the row, used as a fallback.

    Returns:
        The first node name, or the stem of the config filename when Nodes is empty.
    """
    nodes = row.get("Nodes") or []
    if isinstance(nodes, list) and nodes:
        return str(nodes[0])
    return Path(file_name).stem


# File-level parse statuses that mean "Batfish could not produce a usable
# model of this config" — these stay ERROR (block the PR). Everything else
# (PARTIALLY_UNRECOGNIZED in particular) is degraded to WARNING: the file
# parsed, some lines weren't modelled, but the model is still useful for
# downstream checks. Real config bugs surface via `undefinedReferences`,
# not via partial-parse noise from Batfish's grammar gaps.
_PARSE_FATAL_STATUSES: frozenset[str] = frozenset({"PARSE_FAIL", "ORPHANED", "WILL_NOT_COMMIT"})


def findings_from_parse_status(df: pd.DataFrame) -> list[Finding]:
    """Map a pybatfish ``fileParseStatus`` answer into ``Finding`` rows.

    Tiered severity:

    - ``PASSED`` — no finding.
    - ``PARSE_FAIL`` / ``ORPHANED`` / ``WILL_NOT_COMMIT`` — ERROR (file
      couldn't be modelled at all, downstream checks can't run on it).
    - Anything else (in practice ``PARTIALLY_UNRECOGNIZED``) — WARNING
      (file modelled, some lines skipped — usually Batfish grammar gaps
      for vendor features Batfish doesn't yet support).

    Args:
        df: DataFrame with at least ``File_Name``, ``Status``, ``Nodes`` columns.

    Returns:
        One ``Finding`` per non-PASSED row.
    """
    findings: list[Finding] = []
    for _, row in df.iterrows():
        status = str(row["Status"])
        if status == _PARSE_OK:
            continue
        severity: Literal["error", "warning"] = (
            "error" if status in _PARSE_FATAL_STATUSES else "warning"
        )
        file_name = str(row["File_Name"])
        node = _node_from_row(row, file_name)
        findings.append(
            Finding(
                severity=severity,
                query="fileParseStatus",
                node=node,
                message=f"config {file_name} parse status: {status}",
                detail=row.to_dict(),
            )
        )
    return findings


def findings_from_parse_warning(df: pd.DataFrame) -> list[Finding]:
    """Map a pybatfish ``parseWarning`` answer into ``Finding`` WARNING rows.

    These are per-line "Batfish doesn't recognise this construct" notes
    against a file that otherwise parsed. They're informational — Batfish
    grammars lag real vendor syntax, and emitting one as ERROR would mean
    blocking the PR until every grammar gap is closed upstream. Real
    config errors surface as ``undefinedReferences`` (still ERROR) or
    ``fileParseStatus`` with a fatal status (also ERROR).

    Args:
        df: DataFrame with ``Filename``, ``Line``, ``Text``, ``Comment`` columns.

    Returns:
        One ``Finding`` per row, all severity WARNING.
    """
    findings: list[Finding] = []
    for _, row in df.iterrows():
        file_name = str(row["Filename"])
        node = Path(file_name).stem
        line = row["Line"]
        findings.append(
            Finding(
                severity="warning",
                query="parseWarning",
                node=node,
                message=f"parse warning in {file_name} line {line}: {row['Comment']}",
                detail=row.to_dict(),
            )
        )
    return findings


_BGP_OK = "UNIQUE_MATCH"


def findings_from_bgp_session_compat(df: pd.DataFrame) -> list[Finding]:
    """Map a pybatfish ``bgpSessionCompatibility`` answer into ``Finding`` rows.

    Args:
        df: DataFrame with at least ``Node``, ``Remote_Node``, ``Configured_Status`` columns.

    Returns:
        One ``Finding`` per non-UNIQUE_MATCH row, all severity WARNING.
    """
    findings: list[Finding] = []
    for _, row in df.iterrows():
        status = str(row["Configured_Status"])
        if status == _BGP_OK:
            continue
        findings.append(
            Finding(
                severity="warning",
                query="bgpSessionCompatibility",
                node=str(row["Node"]),
                message=(
                    f"bgp session {row['Node']} -> {row['Remote_Node']} "
                    f"(local AS {row['Local_AS']}, remote AS {row['Remote_AS']}): {status}"
                ),
                detail=row.to_dict(),
            )
        )
    return findings


def findings_from_undefined_references(df: pd.DataFrame) -> list[Finding]:
    """Map a pybatfish ``undefinedReferences`` answer into ``Finding`` rows.

    Args:
        df: DataFrame with ``File_Name``, ``Lines``, ``Struct_Type``, ``Ref_Name`` columns.

    Returns:
        One ``Finding`` per row, all severity ERROR.
    """
    findings: list[Finding] = []
    for _, row in df.iterrows():
        file_name = str(row["File_Name"])
        node = Path(file_name).stem
        findings.append(
            Finding(
                severity="error",
                query="undefinedReferences",
                node=node,
                message=(
                    f"undefined {row['Struct_Type']} '{row['Ref_Name']}' referenced in {file_name}"
                ),
                detail=row.to_dict(),
            )
        )
    return findings


def findings_from_isis_edges(df: pd.DataFrame, expected_hosts: set[str]) -> list[Finding]:
    """Map a pybatfish ``isisEdges`` answer into ``Finding`` rows.

    Compares observed directed edges against the expected full mesh among
    ``expected_hosts``. Each missing directed edge is one WARNING finding.

    Args:
        df: DataFrame with ``Interface`` and ``Remote_Interface`` columns,
            each a struct with a ``hostname`` field.
        expected_hosts: Hostnames that should form a full IS-IS mesh.

    Returns:
        One ``Finding`` per missing directed edge.
    """
    observed: set[tuple[str, str]] = set()
    for _, row in df.iterrows():
        local = row["Interface"]
        remote = row["Remote_Interface"]
        local_host = local["hostname"] if isinstance(local, dict) else None
        remote_host = remote["hostname"] if isinstance(remote, dict) else None
        if local_host and remote_host:
            observed.add((local_host, remote_host))

    expected: set[tuple[str, str]] = {
        (a, b) for a in expected_hosts for b in expected_hosts if a != b
    }
    missing = expected - observed

    return [
        Finding(
            severity="warning",
            query="isisEdges",
            node=a,
            message=f"isis adjacency missing: {a} -> {b}",
            detail={"from": a, "to": b},
        )
        for (a, b) in sorted(missing)
    ]


class _PybatfishSession(Protocol):
    """Structural type for the bits of pybatfish.Session we use."""

    def set_network(self, name: str) -> object: ...
    def init_snapshot(self, dir: str, name: str, overwrite: bool) -> object: ...
    def delete_snapshot(self, name: str) -> object: ...

    @property
    def q(self) -> Any: ...


def run_snapshot(
    *,
    session: _PybatfishSession,
    snapshot_dir: Path,
    network: str,
    snapshot_name: str,
    expected_hosts: set[str],
) -> list[Finding]:
    """Initialize a Batfish snapshot, run the query battery, and return findings.

    Always deletes the snapshot in a ``finally`` block, even when queries raise.

    Args:
        session: A connected pybatfish ``Session`` (or any object satisfying
            ``_PybatfishSession``).
        snapshot_dir: Path to the directory containing ``configs/*.cfg``.
        network: Batfish network name (shared across snapshots).
        snapshot_name: Unique per-run snapshot name.
        expected_hosts: PE hostnames that should form a full IS-IS mesh.

    Returns:
        Combined list of findings from all queries.
    """
    session.set_network(network)
    session.init_snapshot(str(snapshot_dir), name=snapshot_name, overwrite=True)
    try:
        findings: list[Finding] = []
        findings.extend(findings_from_parse_status(session.q.fileParseStatus().answer().frame()))
        findings.extend(findings_from_parse_warning(session.q.parseWarning().answer().frame()))
        findings.extend(
            findings_from_undefined_references(session.q.undefinedReferences().answer().frame())
        )
        findings.extend(
            findings_from_bgp_session_compat(session.q.bgpSessionCompatibility().answer().frame())
        )
        findings.extend(
            findings_from_isis_edges(
                session.q.isisEdges().answer().frame(), expected_hosts=expected_hosts
            )
        )
        return findings
    finally:
        session.delete_snapshot(snapshot_name)


def wait_for_batfish(host: str, port: int, timeout_s: float, backoff_s: float) -> bool:
    """Poll the Batfish coordinator until ``/v2/networks`` returns HTTP 200 or timeout elapses.

    Probing ``/`` or ``/networks`` doesn't work — the coordinator returns 404
    on both even when fully up. The API is rooted at ``/v2/`` (matches what
    pybatfish itself hits), and ``/v2/networks`` returns 200 with ``[]`` the
    moment the API is ready, regardless of network/snapshot state.

    Args:
        host: Batfish coordinator hostname.
        port: Coordinator HTTP port (default 9996 for batfish/allinone).
        timeout_s: Total seconds to keep trying before giving up.
        backoff_s: Sleep between attempts.

    Returns:
        True if Batfish responded 200 within the timeout, False otherwise.
    """
    deadline = time.monotonic() + timeout_s
    url = f"http://{host}:{port}/v2/networks"
    while time.monotonic() < deadline:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001 — any failure means "not ready yet"
            pass
        time.sleep(backoff_s)
    return False
