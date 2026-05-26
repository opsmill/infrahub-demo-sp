# Batfish MPLS CI Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Infrahub proposed-change check `batfish_backbone` that loads rendered MPLS backbone configs into a long-lived `batfish/allinone` sidecar, runs parse/undefined-ref/BGP/IS-IS queries via pybatfish, and emits tiered findings (ERROR = parse/undef, WARNING = adjacency, INFO = skipped/missing).

**Architecture:** One check_definition (`BatfishBackboneCheck`) targeting `topologies_mpls`. The check fetches each PE's rendered artifact via the Infrahub SDK, filters out unsupported vendors (Nokia SR Linux + SR OS), writes configs to a temp snapshot dir, initializes a Batfish snapshot, runs the query battery, and maps results to Infrahub check log entries. A separate `batfish_helpers` module owns the Batfish-coupled logic so it's unit-testable in isolation.

**Tech Stack:** Python 3.10+, `infrahub_sdk.checks.InfrahubCheck`, `pybatfish`, `pandas` (transitively), `pytest`, `pytest-asyncio`, Docker Compose. Existing repo uses `uv`, `ruff`, `mypy`. Tests follow `tests/unit/test_checks/test_*.py` pattern already in the repo.

**Reference spec:** `docs/superpowers/specs/2026-05-26-batfish-mpls-ci-validation-design.md`.

---

## File Structure

**New files:**
- `checks/batfish_helpers.py` — `Finding` dataclass, query wrappers, snapshot lifecycle, supported-platform constants. All Batfish/pybatfish coupling lives here.
- `checks/batfish_backbone.py` — `BatfishBackboneCheck` class. Orchestrates: extracts PEs from GraphQL data, fetches artifacts via SDK, calls helpers, maps `Finding` → `log_error`/stdlib logger.
- `queries/validation/batfish_backbone.gql` — Pulls backbone, PEs, platforms.
- `tests/unit/test_checks/test_batfish_helpers.py` — Helpers unit tests.
- `tests/unit/test_checks/test_batfish_backbone.py` — Check class tests.
- `tests/catalog/test_batfish_registration.py` — Catalog registration test.

**Modified files:**
- `.infrahub.yml` — Register the GraphQL query and the check_definition.
- `docker-compose.override.yml` — Add `batfish` service.
- `pyproject.toml` — Add `pybatfish` dep (managed via `uv add`).
- `tasks.py` — Add `batfish-check` invoke task for manual end-to-end runs (optional, included).

---

## Task 1: Add the Batfish sidecar to docker-compose.override.yml

**Files:**
- Modify: `docker-compose.override.yml`

- [ ] **Step 1: Add the batfish service**

Insert the following block at the same indentation level as the existing services (e.g. after the `streamlit-service-catalog` block):

```yaml
  batfish:
    image: batfish/allinone:latest
    container_name: batfish
    restart: unless-stopped
    networks:
      - default
```

No host port binding by default — the check reaches the container on the compose network using DNS name `batfish` and the internal port 9997.

- [ ] **Step 2: Validate the compose file parses**

Run: `docker compose config > /dev/null`
Expected: exits 0 with no output. If you see a YAML error, fix indentation.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.override.yml
git commit -m "compose: add batfish allinone sidecar for proposed-change validation"
```

---

## Task 2: Add pybatfish dependency

**Files:**
- Modify: `pyproject.toml`, `uv.lock`

- [ ] **Step 1: Add the dependency via uv**

Run: `uv add 'pybatfish>=2024.0'`
Expected: `uv` updates `pyproject.toml` and `uv.lock`. The `dependencies` list in `pyproject.toml` now contains `pybatfish>=2024.0`. `pandas` was already present so nothing new transitively (verify).

- [ ] **Step 2: Verify import works**

Run: `uv run python -c "from pybatfish.client.session import Session; print(Session)"`
Expected: prints `<class 'pybatfish.client.session.Session'>` with no import errors.

- [ ] **Step 3: Run mypy on the existing tree to confirm no regression**

Run: `uv run mypy .`
Expected: same result as before this task (no new errors).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add pybatfish for batfish snapshot queries"
```

---

## Task 3: Create the GraphQL query

**Files:**
- Create: `queries/validation/batfish_backbone.gql`

- [ ] **Step 1: Write the query**

Create `queries/validation/batfish_backbone.gql` with:

```graphql
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
                  containerlab_os { value }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

The `$name` variable matches the `topologies_mpls` target group convention: Infrahub passes the backbone name in via the check's `parameters`. We deliberately avoid pulling interface or BGP data here — that lives in the rendered artifact, not in this query.

- [ ] **Step 2: Sanity-check it parses against the live Infrahub instance**

If the stack is up locally:
Run: `uv run infrahubctl query batfish_backbone --variable name=mpls-backbone --branch main`
Expected: JSON shape matches the query (at least one edge, each PE has a platform node).

If the stack isn't up, skip this step — Task 8's catalog test will catch syntax errors.

- [ ] **Step 3: Commit**

```bash
git add queries/validation/batfish_backbone.gql
git commit -m "query: add batfish_backbone graphql for backbone + pe + platform"
```

---

## Task 4: Create the helpers module skeleton with the Finding dataclass

**Files:**
- Create: `checks/batfish_helpers.py`
- Create: `tests/unit/test_checks/test_batfish_helpers.py`

- [ ] **Step 1: Write the failing test for Finding**

Create `tests/unit/test_checks/test_batfish_helpers.py`:

```python
"""Unit tests for batfish helper functions."""

from __future__ import annotations

from checks.batfish_helpers import Finding, SUPPORTED_PLATFORMS


def test_finding_is_constructable() -> None:
    f = Finding(severity="error", query="fileParseStatus", node="pe1", message="boom", detail=None)
    assert f.severity == "error"
    assert f.query == "fileParseStatus"
    assert f.node == "pe1"
    assert f.message == "boom"
    assert f.detail is None


def test_supported_platforms_includes_three_vendors() -> None:
    assert "arista_eos" in SUPPORTED_PLATFORMS
    assert "cisco_iosxr" in SUPPORTED_PLATFORMS
    assert "juniper_junos" in SUPPORTED_PLATFORMS
    assert "nokia_sros" not in SUPPORTED_PLATFORMS
    assert "nokia_srlinux" not in SUPPORTED_PLATFORMS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'checks.batfish_helpers'`

- [ ] **Step 3: Write the minimal implementation**

Create `checks/batfish_helpers.py`:

```python
"""Helpers for the Batfish backbone check.

Owns the pybatfish coupling, snapshot lifecycle, query wrappers, and the
internal ``Finding`` dataclass. The check class in ``batfish_backbone`` uses
these helpers and maps ``Finding`` instances to Infrahub log entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Platforms Batfish parses well. Nokia SR OS support is experimental and SR
# Linux is unsupported — both are filtered out of the snapshot.
SUPPORTED_PLATFORMS: frozenset[str] = frozenset({"arista_eos", "cisco_iosxr", "juniper_junos"})


@dataclass
class Finding:
    """One result row from a Batfish query, normalized for emission.

    Attributes:
        severity: ``error``, ``warning``, or ``info``.
        query: pybatfish query name that produced the finding.
        node: PE hostname the finding is attributed to, if applicable.
        message: One-line human summary.
        detail: Raw row payload (e.g. the pandas Series as dict) for the
            full message body. ``None`` when the finding isn't row-derived.
    """

    severity: Literal["error", "warning", "info"]
    query: str
    node: str | None
    message: str
    detail: dict[str, Any] | None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add checks/batfish_helpers.py tests/unit/test_checks/test_batfish_helpers.py
git commit -m "checks: add batfish helpers skeleton with Finding dataclass"
```

---

## Task 5: Implement `fileParseStatus` query mapping

**Files:**
- Modify: `checks/batfish_helpers.py`
- Modify: `tests/unit/test_checks/test_batfish_helpers.py`

The pybatfish `fileParseStatus` answer is a DataFrame with columns `File_Name`, `Status`, `Nodes`. Good values are `PASSED`. Anything else (`PARTIALLY_UNRECOGNIZED`, `FAILED`, `EMPTY`) is a finding.

- [ ] **Step 1: Write failing tests for `findings_from_parse_status`**

Append to `tests/unit/test_checks/test_batfish_helpers.py`:

```python
import pandas as pd

from checks.batfish_helpers import findings_from_parse_status


def test_parse_status_all_passed_yields_no_findings() -> None:
    df = pd.DataFrame(
        [
            {"File_Name": "configs/pe1.cfg", "Status": "PASSED", "Nodes": ["pe1"]},
            {"File_Name": "configs/pe2.cfg", "Status": "PASSED", "Nodes": ["pe2"]},
        ]
    )
    findings = findings_from_parse_status(df)
    assert findings == []


def test_parse_status_failed_yields_one_error_per_bad_row() -> None:
    df = pd.DataFrame(
        [
            {"File_Name": "configs/pe1.cfg", "Status": "PASSED", "Nodes": ["pe1"]},
            {"File_Name": "configs/pe2.cfg", "Status": "PARTIALLY_UNRECOGNIZED", "Nodes": ["pe2"]},
            {"File_Name": "configs/pe3.cfg", "Status": "FAILED", "Nodes": []},
        ]
    )
    findings = findings_from_parse_status(df)
    assert len(findings) == 2
    assert {f.node for f in findings} == {"pe2", "pe3"}
    assert all(f.severity == "error" for f in findings)
    assert all(f.query == "fileParseStatus" for f in findings)
    # pe3 had no Nodes — message should still reference the file.
    pe3 = next(f for f in findings if f.node == "pe3")
    assert "configs/pe3.cfg" in pe3.message
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: 2 failures with `ImportError: cannot import name 'findings_from_parse_status'`.

- [ ] **Step 3: Implement `findings_from_parse_status`**

Append to `checks/batfish_helpers.py`:

```python
import pandas as pd

_PARSE_OK = "PASSED"


def _node_from_row(row: pd.Series, file_name: str) -> str:
    """Pull the first node from a parse-status row, or fall back to the filename stem."""
    nodes = row.get("Nodes") or []
    if isinstance(nodes, list) and nodes:
        return str(nodes[0])
    # Fall back: stem of "configs/<hostname>.cfg"
    return file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def findings_from_parse_status(df: pd.DataFrame) -> list[Finding]:
    """Map a pybatfish ``fileParseStatus`` answer into ``Finding`` rows.

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
        file_name = str(row["File_Name"])
        node = _node_from_row(row, file_name)
        findings.append(
            Finding(
                severity="error",
                query="fileParseStatus",
                node=node,
                message=f"config {file_name} parse status: {status}",
                detail=row.to_dict(),
            )
        )
    return findings
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add checks/batfish_helpers.py tests/unit/test_checks/test_batfish_helpers.py
git commit -m "checks: map batfish fileParseStatus into Finding rows"
```

---

## Task 6: Implement `parseWarning` query mapping

**Files:**
- Modify: `checks/batfish_helpers.py`
- Modify: `tests/unit/test_checks/test_batfish_helpers.py`

The pybatfish `parseWarning` answer has columns `Filename`, `Line`, `Text`, `Comment`, `Parser_Context`. Every row is a finding (severity ERROR).

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_checks/test_batfish_helpers.py`:

```python
from checks.batfish_helpers import findings_from_parse_warning


def test_parse_warning_empty_yields_no_findings() -> None:
    df = pd.DataFrame(columns=["Filename", "Line", "Text", "Comment", "Parser_Context"])
    assert findings_from_parse_warning(df) == []


def test_parse_warning_populated_yields_one_error_per_row() -> None:
    df = pd.DataFrame(
        [
            {
                "Filename": "configs/pe1.cfg",
                "Line": 42,
                "Text": "platform-specific-knob foo",
                "Comment": "This syntax is unrecognized",
                "Parser_Context": "some context",
            }
        ]
    )
    findings = findings_from_parse_warning(df)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "error"
    assert f.query == "parseWarning"
    assert f.node == "pe1"
    assert "line 42" in f.message
    assert "configs/pe1.cfg" in f.message
    assert f.detail is not None and f.detail["Text"] == "platform-specific-knob foo"
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py::test_parse_warning_empty_yields_no_findings tests/unit/test_checks/test_batfish_helpers.py::test_parse_warning_populated_yields_one_error_per_row -v`
Expected: 2 failures with `ImportError`.

- [ ] **Step 3: Implement `findings_from_parse_warning`**

Append to `checks/batfish_helpers.py`:

```python
def findings_from_parse_warning(df: pd.DataFrame) -> list[Finding]:
    """Map a pybatfish ``parseWarning`` answer into ``Finding`` rows.

    Args:
        df: DataFrame with ``Filename``, ``Line``, ``Text``, ``Comment`` columns.

    Returns:
        One ``Finding`` per row, all severity ERROR.
    """
    findings: list[Finding] = []
    for _, row in df.iterrows():
        file_name = str(row["Filename"])
        node = file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        line = row["Line"]
        findings.append(
            Finding(
                severity="error",
                query="parseWarning",
                node=node,
                message=f"parse warning in {file_name} line {line}: {row['Comment']}",
                detail=row.to_dict(),
            )
        )
    return findings
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: all tests in the file pass.

- [ ] **Step 5: Commit**

```bash
git add checks/batfish_helpers.py tests/unit/test_checks/test_batfish_helpers.py
git commit -m "checks: map batfish parseWarning into Finding rows"
```

---

## Task 7: Implement `undefinedReferences` query mapping

**Files:**
- Modify: `checks/batfish_helpers.py`
- Modify: `tests/unit/test_checks/test_batfish_helpers.py`

The pybatfish `undefinedReferences` answer has columns `File_Name`, `Lines`, `Type` (the kind of structure), `Structure_Name`, `Context`. Every row is an ERROR.

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_checks/test_batfish_helpers.py`:

```python
from checks.batfish_helpers import findings_from_undefined_references


def test_undefined_refs_empty_yields_no_findings() -> None:
    df = pd.DataFrame(columns=["File_Name", "Lines", "Type", "Structure_Name", "Context"])
    assert findings_from_undefined_references(df) == []


def test_undefined_refs_populated_yields_one_error_per_row() -> None:
    df = pd.DataFrame(
        [
            {
                "File_Name": "configs/pe1.cfg",
                "Lines": [120, 121],
                "Type": "route-map",
                "Structure_Name": "RM-EXPORT-MISSING",
                "Context": "bgp-neighbor-export",
            }
        ]
    )
    findings = findings_from_undefined_references(df)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "error"
    assert f.query == "undefinedReferences"
    assert f.node == "pe1"
    assert "RM-EXPORT-MISSING" in f.message
    assert "route-map" in f.message
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: 2 new failures with `ImportError`.

- [ ] **Step 3: Implement `findings_from_undefined_references`**

Append to `checks/batfish_helpers.py`:

```python
def findings_from_undefined_references(df: pd.DataFrame) -> list[Finding]:
    """Map a pybatfish ``undefinedReferences`` answer into ``Finding`` rows.

    Args:
        df: DataFrame with ``File_Name``, ``Lines``, ``Type``, ``Structure_Name`` columns.

    Returns:
        One ``Finding`` per row, all severity ERROR.
    """
    findings: list[Finding] = []
    for _, row in df.iterrows():
        file_name = str(row["File_Name"])
        node = file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        findings.append(
            Finding(
                severity="error",
                query="undefinedReferences",
                node=node,
                message=(
                    f"undefined {row['Type']} '{row['Structure_Name']}' "
                    f"referenced in {file_name}"
                ),
                detail=row.to_dict(),
            )
        )
    return findings
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: all tests in the file pass.

- [ ] **Step 5: Commit**

```bash
git add checks/batfish_helpers.py tests/unit/test_checks/test_batfish_helpers.py
git commit -m "checks: map batfish undefinedReferences into Finding rows"
```

---

## Task 8: Implement `bgpSessionCompatibility` query mapping

**Files:**
- Modify: `checks/batfish_helpers.py`
- Modify: `tests/unit/test_checks/test_batfish_helpers.py`

The pybatfish `bgpSessionCompatibility` answer has columns including `Node`, `Remote_Node`, `Local_AS`, `Remote_AS`, `Configured_Status`. `UNIQUE_MATCH` is good; anything else (`HALF_OPEN`, `NO_MATCH_FOUND`, `MULTIPLE_REMOTES`, `MISSING_LOCAL_AS`, `NO_REMOTE_AS`) is a WARNING finding.

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_checks/test_batfish_helpers.py`:

```python
from checks.batfish_helpers import findings_from_bgp_session_compat


def test_bgp_compat_all_unique_match_yields_no_findings() -> None:
    df = pd.DataFrame(
        [
            {
                "Node": "pe1",
                "Remote_Node": "pe2",
                "Local_AS": 65000,
                "Remote_AS": 65000,
                "Configured_Status": "UNIQUE_MATCH",
            }
        ]
    )
    assert findings_from_bgp_session_compat(df) == []


def test_bgp_compat_half_open_yields_warning() -> None:
    df = pd.DataFrame(
        [
            {
                "Node": "pe1",
                "Remote_Node": "pe2",
                "Local_AS": 65000,
                "Remote_AS": 65000,
                "Configured_Status": "HALF_OPEN",
            },
            {
                "Node": "pe1",
                "Remote_Node": "pe3",
                "Local_AS": 65000,
                "Remote_AS": 65001,
                "Configured_Status": "NO_MATCH_FOUND",
            },
        ]
    )
    findings = findings_from_bgp_session_compat(df)
    assert len(findings) == 2
    assert all(f.severity == "warning" for f in findings)
    assert all(f.query == "bgpSessionCompatibility" for f in findings)
    assert all(f.node == "pe1" for f in findings)
    half_open = next(f for f in findings if "HALF_OPEN" in f.message)
    assert "pe2" in half_open.message
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: 2 new failures with `ImportError`.

- [ ] **Step 3: Implement `findings_from_bgp_session_compat`**

Append to `checks/batfish_helpers.py`:

```python
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
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: all tests in the file pass.

- [ ] **Step 5: Commit**

```bash
git add checks/batfish_helpers.py tests/unit/test_checks/test_batfish_helpers.py
git commit -m "checks: map batfish bgpSessionCompatibility into warning Finding rows"
```

---

## Task 9: Implement `isisEdges` adjacency mapping with expected mesh comparison

**Files:**
- Modify: `checks/batfish_helpers.py`
- Modify: `tests/unit/test_checks/test_batfish_helpers.py`

pybatfish's `isisEdges` returns columns `Interface` and `Remote_Interface`, each a struct with a `hostname` field. The check has the expected full-mesh edge set (every supported PE → every other supported PE) computed from the backbone PE list. Missing edges become WARNING findings.

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_checks/test_batfish_helpers.py`:

```python
from checks.batfish_helpers import findings_from_isis_edges


def _iface(hostname: str) -> dict:
    return {"hostname": hostname, "interface": "irrelevant"}


def test_isis_edges_full_mesh_yields_no_findings() -> None:
    # 3 PEs with all 6 directed edges present.
    rows = []
    pes = ["pe1", "pe2", "pe3"]
    for a in pes:
        for b in pes:
            if a == b:
                continue
            rows.append({"Interface": _iface(a), "Remote_Interface": _iface(b)})
    df = pd.DataFrame(rows)
    findings = findings_from_isis_edges(df, expected_hosts=set(pes))
    assert findings == []


def test_isis_edges_missing_edge_yields_one_warning() -> None:
    # 3 PEs, missing pe1 -> pe3 and pe3 -> pe1.
    pes = ["pe1", "pe2", "pe3"]
    rows = [
        {"Interface": _iface("pe1"), "Remote_Interface": _iface("pe2")},
        {"Interface": _iface("pe2"), "Remote_Interface": _iface("pe1")},
        {"Interface": _iface("pe2"), "Remote_Interface": _iface("pe3")},
        {"Interface": _iface("pe3"), "Remote_Interface": _iface("pe2")},
    ]
    df = pd.DataFrame(rows)
    findings = findings_from_isis_edges(df, expected_hosts=set(pes))
    # Two missing directed edges: (pe1, pe3) and (pe3, pe1).
    assert len(findings) == 2
    assert all(f.severity == "warning" for f in findings)
    assert all(f.query == "isisEdges" for f in findings)
    pairs = {(f.detail["from"], f.detail["to"]) for f in findings if f.detail}
    assert pairs == {("pe1", "pe3"), ("pe3", "pe1")}


def test_isis_edges_empty_with_no_expected_hosts_passes() -> None:
    df = pd.DataFrame(columns=["Interface", "Remote_Interface"])
    assert findings_from_isis_edges(df, expected_hosts=set()) == []
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: 3 new failures with `ImportError`.

- [ ] **Step 3: Implement `findings_from_isis_edges`**

Append to `checks/batfish_helpers.py`:

```python
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
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: all tests in the file pass.

- [ ] **Step 5: Commit**

```bash
git add checks/batfish_helpers.py tests/unit/test_checks/test_batfish_helpers.py
git commit -m "checks: detect missing isis adjacencies against expected mesh"
```

---

## Task 10: Implement snapshot lifecycle helper (`run_snapshot`)

**Files:**
- Modify: `checks/batfish_helpers.py`
- Modify: `tests/unit/test_checks/test_batfish_helpers.py`

This is the function the check class calls once it has the configs in a tempdir. It wires up health-check polling, snapshot init, runs the query battery, deletes the snapshot in `finally`, and returns the aggregated `Finding` list. Pybatfish's `Session` is injected as a callable so the test can pass a fake.

- [ ] **Step 1: Write failing tests for `run_snapshot`**

Append to `tests/unit/test_checks/test_batfish_helpers.py`:

```python
from unittest.mock import MagicMock

from checks.batfish_helpers import run_snapshot


def _fake_session_factory(answers: dict[str, pd.DataFrame]) -> MagicMock:
    """Build a fake pybatfish Session that returns canned DataFrames per query."""
    session = MagicMock()
    session.q.fileParseStatus.return_value.answer.return_value.frame.return_value = (
        answers.get("fileParseStatus", pd.DataFrame(columns=["File_Name", "Status", "Nodes"]))
    )
    session.q.parseWarning.return_value.answer.return_value.frame.return_value = (
        answers.get(
            "parseWarning",
            pd.DataFrame(columns=["Filename", "Line", "Text", "Comment", "Parser_Context"]),
        )
    )
    session.q.undefinedReferences.return_value.answer.return_value.frame.return_value = (
        answers.get(
            "undefinedReferences",
            pd.DataFrame(columns=["File_Name", "Lines", "Type", "Structure_Name", "Context"]),
        )
    )
    session.q.bgpSessionCompatibility.return_value.answer.return_value.frame.return_value = (
        answers.get(
            "bgpSessionCompatibility",
            pd.DataFrame(
                columns=["Node", "Remote_Node", "Local_AS", "Remote_AS", "Configured_Status"]
            ),
        )
    )
    session.q.isisEdges.return_value.answer.return_value.frame.return_value = answers.get(
        "isisEdges", pd.DataFrame(columns=["Interface", "Remote_Interface"])
    )
    return session


def test_run_snapshot_happy_path_no_findings(tmp_path) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "pe1.cfg").write_text("! pe1\n")
    session = _fake_session_factory({})
    findings = run_snapshot(
        session=session,
        snapshot_dir=tmp_path,
        network="infrahub-mpls",
        snapshot_name="snap-1",
        expected_hosts={"pe1"},
    )
    assert findings == []
    session.init_snapshot.assert_called_once_with(
        str(tmp_path), name="snap-1", overwrite=True
    )
    session.delete_snapshot.assert_called_once_with("snap-1")


def test_run_snapshot_failed_parse_yields_error_finding(tmp_path) -> None:
    (tmp_path / "configs").mkdir()
    session = _fake_session_factory(
        {
            "fileParseStatus": pd.DataFrame(
                [{"File_Name": "configs/pe1.cfg", "Status": "FAILED", "Nodes": ["pe1"]}]
            )
        }
    )
    findings = run_snapshot(
        session=session,
        snapshot_dir=tmp_path,
        network="infrahub-mpls",
        snapshot_name="snap-2",
        expected_hosts={"pe1"},
    )
    assert any(f.severity == "error" and f.query == "fileParseStatus" for f in findings)
    session.delete_snapshot.assert_called_once_with("snap-2")


def test_run_snapshot_deletes_on_query_exception(tmp_path) -> None:
    (tmp_path / "configs").mkdir()
    session = _fake_session_factory({})
    session.q.fileParseStatus.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        run_snapshot(
            session=session,
            snapshot_dir=tmp_path,
            network="infrahub-mpls",
            snapshot_name="snap-3",
            expected_hosts={"pe1"},
        )
    session.delete_snapshot.assert_called_once_with("snap-3")


import pytest  # noqa: E402  (used by the test above)
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: 3 new failures with `ImportError: cannot import name 'run_snapshot'`.

- [ ] **Step 3: Implement `run_snapshot`**

Append to `checks/batfish_helpers.py`:

```python
from pathlib import Path
from typing import Protocol


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
            findings_from_undefined_references(
                session.q.undefinedReferences().answer().frame()
            )
        )
        findings.extend(
            findings_from_bgp_session_compat(
                session.q.bgpSessionCompatibility().answer().frame()
            )
        )
        findings.extend(
            findings_from_isis_edges(
                session.q.isisEdges().answer().frame(), expected_hosts=expected_hosts
            )
        )
        return findings
    finally:
        session.delete_snapshot(snapshot_name)
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add checks/batfish_helpers.py tests/unit/test_checks/test_batfish_helpers.py
git commit -m "checks: add run_snapshot orchestrator with finally cleanup"
```

---

## Task 11: Add the Batfish health-check probe

**Files:**
- Modify: `checks/batfish_helpers.py`
- Modify: `tests/unit/test_checks/test_batfish_helpers.py`

Helper that polls Batfish's HTTP health endpoint until 200 OK or timeout. The check uses it before constructing a `Session`.

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_checks/test_batfish_helpers.py`:

```python
from unittest.mock import patch

from checks.batfish_helpers import wait_for_batfish


def test_wait_for_batfish_returns_true_on_first_200() -> None:
    with patch("checks.batfish_helpers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        assert wait_for_batfish("batfish", port=9997, timeout_s=10, backoff_s=0.01) is True
        assert mock_get.call_count == 1


def test_wait_for_batfish_returns_false_after_timeout() -> None:
    with patch("checks.batfish_helpers.requests.get") as mock_get:
        mock_get.side_effect = Exception("connection refused")
        assert wait_for_batfish("batfish", port=9997, timeout_s=0.1, backoff_s=0.05) is False
        assert mock_get.call_count >= 1
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: 2 new failures with `ImportError`.

- [ ] **Step 3: Implement `wait_for_batfish`**

Add at the top of `checks/batfish_helpers.py` (after the existing imports):

```python
import time

import requests
```

Append:

```python
def wait_for_batfish(host: str, port: int, timeout_s: float, backoff_s: float) -> bool:
    """Poll the Batfish coordinator until it returns HTTP 200 or timeout elapses.

    Args:
        host: Batfish coordinator hostname.
        port: Coordinator HTTP port (default 9997).
        timeout_s: Total seconds to keep trying before giving up.
        backoff_s: Sleep between attempts.

    Returns:
        True if Batfish responded 200 within the timeout, False otherwise.
    """
    deadline = time.monotonic() + timeout_s
    url = f"http://{host}:{port}/"
    while time.monotonic() < deadline:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001 — any failure means "not ready yet"
            pass
        time.sleep(backoff_s)
    return False
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/unit/test_checks/test_batfish_helpers.py -v`
Expected: all tests in the file pass.

- [ ] **Step 5: Verify mypy still clean**

Run: `uv run mypy checks/batfish_helpers.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add checks/batfish_helpers.py tests/unit/test_checks/test_batfish_helpers.py
git commit -m "checks: add wait_for_batfish health probe"
```

---

## Task 12: Implement the `BatfishBackboneCheck` class — happy path

**Files:**
- Create: `checks/batfish_backbone.py`
- Create: `tests/unit/test_checks/test_batfish_backbone.py`

This is the orchestrator that ties everything together. Start with a minimal happy-path test before adding edge cases.

- [ ] **Step 1: Write the failing happy-path test**

Create `tests/unit/test_checks/test_batfish_backbone.py`:

```python
"""Unit tests for the BatfishBackboneCheck."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from checks.batfish_backbone import BatfishBackboneCheck


def _pe(name: str, platform: str) -> dict:
    return {
        "node": {
            "id": f"id-{name}",
            "name": {"value": name},
            "platform": {"node": {"name": {"value": platform}, "containerlab_os": {"value": ""}}},
        }
    }


def _backbone_data(pes: list[dict]) -> dict:
    return {
        "TopologyMplsBackbone": {
            "edges": [{"node": {"name": {"value": "mpls-backbone"}, "pes": {"edges": pes}}}]
        }
    }


@pytest.mark.asyncio
async def test_happy_path_no_findings() -> None:
    """Three supported PEs, all configs parse cleanly, no findings → no errors."""
    data = _backbone_data(
        [_pe("pe1", "arista_eos"), _pe("pe2", "cisco_iosxr"), _pe("pe3", "juniper_junos")]
    )
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock(return_value="! config\n")  # type: ignore[attr-defined]

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=True),
        patch("checks.batfish_backbone.Session") as session_cls,
        patch("checks.batfish_backbone.run_snapshot", return_value=[]) as run_snap,
    ):
        await check.validate(data)

    assert check.errors == []
    assert session_cls.called
    assert run_snap.called
    # Snapshot received configs for all three PEs.
    snapshot_dir = run_snap.call_args.kwargs["snapshot_dir"]
    config_files = sorted((snapshot_dir / "configs").iterdir())
    assert [p.name for p in config_files] == ["pe1.cfg", "pe2.cfg", "pe3.cfg"]
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `uv run pytest tests/unit/test_checks/test_batfish_backbone.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'checks.batfish_backbone'`.

- [ ] **Step 3: Implement the minimal `BatfishBackboneCheck`**

Create `checks/batfish_backbone.py`:

```python
"""Batfish-driven validation check for the MPLS backbone.

Runs on every Infrahub proposed change targeting ``topologies_mpls``. For each
backbone, fetches per-PE rendered configs via the Infrahub SDK, filters out
unsupported vendors, loads the configs into a temporary Batfish snapshot, runs
the query battery, and maps findings to Infrahub log entries.

See ``docs/superpowers/specs/2026-05-26-batfish-mpls-ci-validation-design.md``.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from infrahub_sdk.checks import InfrahubCheck
from pybatfish.client.session import Session

from checks.batfish_helpers import (
    SUPPORTED_PLATFORMS,
    Finding,
    run_snapshot,
    wait_for_batfish,
)

logger = logging.getLogger(__name__)

_ARTIFACT_NAME_BY_PLATFORM = {
    "arista_eos": "pe-arista-eos",
    "cisco_iosxr": "pe-cisco-iosxr",
    "juniper_junos": "pe-juniper-junos",
}


class BatfishBackboneCheck(InfrahubCheck):
    """Validate every MPLS backbone with Batfish."""

    query = "batfish_backbone"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Run Batfish queries against each backbone in ``data`` and log findings.

        Args:
            data: Result of the ``batfish_backbone`` GraphQL query, scoped to
                one backbone per invocation by the ``topologies_mpls`` target.
        """
        if os.environ.get("BATFISH_DISABLED") == "1":
            logger.info("Batfish disabled by environment")
            return

        for edge in data.get("TopologyMplsBackbone", {}).get("edges", []):
            await self._validate_backbone(edge["node"])

    async def _validate_backbone(self, backbone: dict[str, Any]) -> None:
        backbone_name = backbone["name"]["value"]
        supported_pes, skipped = self._partition_pes(backbone)

        for pe_name, platform_name in skipped:
            logger.info(
                "skipping %s: batfish does not support platform %s", pe_name, platform_name
            )

        if not supported_pes:
            logger.info("no supported PEs to validate in backbone %s", backbone_name)
            return

        with tempfile.TemporaryDirectory(prefix=f"batfish-{backbone_name}-") as tmp:
            tmp_path = Path(tmp)
            configs_dir = tmp_path / "configs"
            configs_dir.mkdir()

            hosts_in_snapshot: set[str] = set()
            for pe_id, pe_name, platform_name in supported_pes:
                body = await self._fetch_artifact(pe_id=pe_id, platform_name=platform_name)
                if body is None:
                    logger.info("no artifact yet for %s — skipping in snapshot", pe_name)
                    continue
                (configs_dir / f"{pe_name}.cfg").write_text(body)
                hosts_in_snapshot.add(pe_name)

            if not hosts_in_snapshot:
                logger.info("no PE artifacts available for backbone %s", backbone_name)
                return

            host = os.environ.get("BATFISH_HOST", "batfish")
            port = int(os.environ.get("BATFISH_PORT", "9997"))
            if not wait_for_batfish(host, port=port, timeout_s=60, backoff_s=2):
                self.log_error(message=f"Batfish service unreachable at {host}:{port}")
                return

            session = Session(host=host)
            snapshot_name = f"{backbone_name}-{uuid.uuid4().hex[:8]}"
            findings = run_snapshot(
                session=session,
                snapshot_dir=tmp_path,
                network="infrahub-mpls",
                snapshot_name=snapshot_name,
                expected_hosts=hosts_in_snapshot,
            )
            self._emit_findings(findings)

    def _partition_pes(
        self, backbone: dict[str, Any]
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
        """Split PEs into (supported, skipped) based on platform.

        Returns:
            Tuple of (supported, skipped) where supported is a list of
            ``(pe_id, pe_name, platform_name)`` and skipped is ``(pe_name, platform_name)``.
        """
        supported: list[tuple[str, str, str]] = []
        skipped: list[tuple[str, str]] = []
        for pe_edge in backbone.get("pes", {}).get("edges", []):
            pe = pe_edge["node"]
            platform_node = (pe.get("platform") or {}).get("node") or {}
            platform_name = (platform_node.get("name") or {}).get("value") or ""
            pe_name = pe["name"]["value"]
            pe_id = pe["id"]
            if platform_name in SUPPORTED_PLATFORMS:
                supported.append((pe_id, pe_name, platform_name))
            else:
                skipped.append((pe_name, platform_name))
        return supported, skipped

    async def _fetch_artifact(self, pe_id: str, platform_name: str) -> str | None:
        """Fetch the latest rendered config artifact for ``pe_id``.

        Args:
            pe_id: Infrahub node id of the PE device.
            platform_name: Platform name used to choose the artifact definition.

        Returns:
            The artifact body as text, or None if the artifact does not exist.
        """
        artifact_def = _ARTIFACT_NAME_BY_PLATFORM[platform_name]
        try:
            artifact = await self.client.get(
                kind="CoreArtifact",
                object__ids=[pe_id],
                definition__name__value=artifact_def,
            )
        except Exception:  # noqa: BLE001 — SDK raises a variety of "not found" errors
            return None
        storage_id_attr = getattr(artifact, "storage_id", None)
        storage_id = storage_id_attr.value if storage_id_attr is not None else None
        if not storage_id:
            return None
        body = await self.client.object_store.get(identifier=storage_id)
        if isinstance(body, bytes):
            return body.decode("utf-8")
        return str(body)

    def _emit_findings(self, findings: list[Finding]) -> None:
        """Map findings to Infrahub log entries.

        ERROR findings call ``log_error`` (which fails the check). WARNING and
        INFO findings go to the stdlib logger so they appear in check
        execution logs but do not fail the check.
        """
        for f in findings:
            if f.severity == "error":
                self.log_error(message=f"[{f.query}] {f.message}")
            elif f.severity == "warning":
                logger.warning("[%s] %s", f.query, f.message)
            else:
                logger.info("[%s] %s", f.query, f.message)
```

- [ ] **Step 4: Run test to confirm it passes**

Run: `uv run pytest tests/unit/test_checks/test_batfish_backbone.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add checks/batfish_backbone.py tests/unit/test_checks/test_batfish_backbone.py
git commit -m "checks: add BatfishBackboneCheck happy path"
```

---

## Task 13: Cover edge cases — Nokia skipped, missing artifact, empty snapshot, disabled env

**Files:**
- Modify: `tests/unit/test_checks/test_batfish_backbone.py`

- [ ] **Step 1: Add edge-case tests**

Append to `tests/unit/test_checks/test_batfish_backbone.py`:

```python
@pytest.mark.asyncio
async def test_nokia_pes_skipped_from_snapshot() -> None:
    data = _backbone_data(
        [_pe("pe1", "arista_eos"), _pe("pe2", "nokia_sros"), _pe("pe3", "nokia_srlinux")]
    )
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock(return_value="! config\n")  # type: ignore[attr-defined]

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=True),
        patch("checks.batfish_backbone.Session"),
        patch("checks.batfish_backbone.run_snapshot", return_value=[]) as run_snap,
    ):
        await check.validate(data)

    snapshot_dir = run_snap.call_args.kwargs["snapshot_dir"]
    config_files = sorted((snapshot_dir / "configs").iterdir())
    assert [p.name for p in config_files] == ["pe1.cfg"]
    assert check.errors == []


@pytest.mark.asyncio
async def test_missing_artifact_excluded_but_does_not_fail() -> None:
    data = _backbone_data([_pe("pe1", "arista_eos"), _pe("pe2", "cisco_iosxr")])
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    # pe1 has no artifact yet, pe2 does.
    check._fetch_artifact = AsyncMock(side_effect=[None, "! pe2\n"])  # type: ignore[attr-defined]

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=True),
        patch("checks.batfish_backbone.Session"),
        patch("checks.batfish_backbone.run_snapshot", return_value=[]) as run_snap,
    ):
        await check.validate(data)

    snapshot_dir = run_snap.call_args.kwargs["snapshot_dir"]
    config_files = sorted((snapshot_dir / "configs").iterdir())
    assert [p.name for p in config_files] == ["pe2.cfg"]
    assert check.errors == []


@pytest.mark.asyncio
async def test_all_pes_skipped_short_circuits() -> None:
    data = _backbone_data([_pe("pe1", "nokia_sros"), _pe("pe2", "nokia_srlinux")])
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock()  # type: ignore[attr-defined]

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=True),
        patch("checks.batfish_backbone.Session") as session_cls,
        patch("checks.batfish_backbone.run_snapshot") as run_snap,
    ):
        await check.validate(data)

    # Snapshot was never initialized.
    assert not session_cls.called
    assert not run_snap.called
    assert check.errors == []


@pytest.mark.asyncio
async def test_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("BATFISH_DISABLED", "1")
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock()  # type: ignore[attr-defined]

    with patch("checks.batfish_backbone.Session") as session_cls:
        await check.validate(data)

    assert not session_cls.called
    assert check.errors == []


@pytest.mark.asyncio
async def test_batfish_unreachable_logs_error() -> None:
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock(return_value="! pe1\n")  # type: ignore[attr-defined]

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=False),
        patch("checks.batfish_backbone.Session") as session_cls,
        patch("checks.batfish_backbone.run_snapshot") as run_snap,
    ):
        await check.validate(data)

    assert not session_cls.called
    assert not run_snap.called
    assert len(check.errors) == 1
    assert "unreachable" in check.errors[0]["message"]


@pytest.mark.asyncio
async def test_error_findings_become_check_errors() -> None:
    data = _backbone_data([_pe("pe1", "arista_eos")])
    check = BatfishBackboneCheck(branch="main")
    check.client = MagicMock()
    check._fetch_artifact = AsyncMock(return_value="! pe1\n")  # type: ignore[attr-defined]

    error_finding = Finding(
        severity="error",
        query="fileParseStatus",
        node="pe1",
        message="config configs/pe1.cfg parse status: FAILED",
        detail=None,
    )
    warning_finding = Finding(
        severity="warning",
        query="bgpSessionCompatibility",
        node="pe1",
        message="bgp half open",
        detail=None,
    )

    with (
        patch("checks.batfish_backbone.wait_for_batfish", return_value=True),
        patch("checks.batfish_backbone.Session"),
        patch(
            "checks.batfish_backbone.run_snapshot",
            return_value=[error_finding, warning_finding],
        ),
    ):
        await check.validate(data)

    assert len(check.errors) == 1
    assert "fileParseStatus" in check.errors[0]["message"]


from checks.batfish_helpers import Finding  # noqa: E402  (referenced above)
```

- [ ] **Step 2: Run all check tests**

Run: `uv run pytest tests/unit/test_checks/test_batfish_backbone.py -v`
Expected: 7 passed (1 from Task 12 + 6 here).

- [ ] **Step 3: Run full unit suite to verify no regressions**

Run: `uv run pytest tests/unit/ -v`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_checks/test_batfish_backbone.py
git commit -m "checks: cover batfish skip/missing/disabled/unreachable paths"
```

---

## Task 14: Register the query and check in `.infrahub.yml`

**Files:**
- Modify: `.infrahub.yml`

- [ ] **Step 1: Register the GraphQL query**

In `.infrahub.yml`, find the `queries:` list. Append:

```yaml
  - {name: batfish_backbone, file_path: queries/validation/batfish_backbone.gql}
```

Keep the existing entries unchanged.

- [ ] **Step 2: Register the check_definition**

Find the `check_definitions:` list. Append:

```yaml
  - name: batfish_backbone
    class_name: BatfishBackboneCheck
    file_path: checks/batfish_backbone.py
    targets: topologies_mpls
    parameters:
      name: name__value
```

- [ ] **Step 3: Validate yaml syntax**

Run: `uv run yamllint .infrahub.yml`
Expected: exit 0.

- [ ] **Step 4: Validate schema against Infrahub conventions (if the stack is up)**

Run: `uv run infrahubctl schema check` (skip if Infrahub isn't running locally)
Expected: success / no schema-side errors. The check definition itself isn't schema, so the more relevant guard is the catalog test in Task 15.

- [ ] **Step 5: Commit**

```bash
git add .infrahub.yml
git commit -m "infrahub: register batfish_backbone query + check_definition"
```

---

## Task 15: Add the catalog registration test

**Files:**
- Create: `tests/catalog/test_batfish_registration.py`

- [ ] **Step 1: Write the test**

Create `tests/catalog/test_batfish_registration.py`:

```python
"""Catalog test: ensure batfish_backbone is registered in .infrahub.yml."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_batfish_query_registered() -> None:
    cfg = yaml.safe_load(Path(".infrahub.yml").read_text())
    queries = {q["name"]: q for q in cfg.get("queries", [])}
    assert "batfish_backbone" in queries
    assert queries["batfish_backbone"]["file_path"] == "queries/validation/batfish_backbone.gql"
    assert Path(queries["batfish_backbone"]["file_path"]).exists()


def test_batfish_check_registered() -> None:
    cfg = yaml.safe_load(Path(".infrahub.yml").read_text())
    checks = {c["name"]: c for c in cfg.get("check_definitions", [])}
    assert "batfish_backbone" in checks
    c = checks["batfish_backbone"]
    assert c["class_name"] == "BatfishBackboneCheck"
    assert c["file_path"] == "checks/batfish_backbone.py"
    assert c["targets"] == "topologies_mpls"
    assert Path(c["file_path"]).exists()
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/catalog/test_batfish_registration.py -v`
Expected: 2 passed.

- [ ] **Step 3: Run the full catalog suite**

Run: `uv run pytest tests/catalog/ -v`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/catalog/test_batfish_registration.py
git commit -m "tests: catalog test for batfish_backbone registration"
```

---

## Task 16: Add manual invoke task for end-to-end Batfish runs

**Files:**
- Modify: `tasks.py`

The catalog/unit tests never contact a real Batfish. This task adds an opt-in invoke target the engineer can run locally to verify the wiring end-to-end.

- [ ] **Step 1: Locate where existing invoke tasks live**

Run: `grep -n "^@task" tasks.py | head -10`
Expected: a list of decorated functions. Place the new task near the other validation-oriented ones.

- [ ] **Step 2: Add the new task**

Append to `tasks.py`:

```python
@task
def batfish_check(c: Context, backbone: str = "mpls-backbone") -> None:
    """Run BatfishBackboneCheck against a running local Infrahub.

    Requires:
        - `uv run invoke start` (Infrahub + Batfish sidecar up)
        - INFRAHUB_ADDRESS, INFRAHUB_API_TOKEN set in .env
        - At least one rendered pe-* artifact in the local instance

    Args:
        c: Invoke Context.
        backbone: Backbone name to validate (default: mpls-backbone).
    """
    cmd = (
        "uv run infrahubctl check batfish_backbone "
        f"--variable name={backbone} --branch main"
    )
    c.run(cmd, pty=True)
```

If `Context` and `task` aren't already imported at the top of `tasks.py`, add them — but in this repo they almost certainly are (check the file's import block before adding).

- [ ] **Step 3: Verify the task is discoverable**

Run: `uv run invoke --list | grep batfish`
Expected: `  batfish-check ...` appears.

- [ ] **Step 4: Commit**

```bash
git add tasks.py
git commit -m "tasks: add batfish-check invoke target for manual e2e runs"
```

---

## Task 17: Run the full lint + test suite and fix anything that surfaces

**Files:**
- Potentially: any new file

- [ ] **Step 1: Run the project's lint suite**

Run: `uv run invoke lint`
Expected: ruff, mypy, yamllint all clean. If a docstring or type hint is missing, add it. If mypy complains about pybatfish stubs, add `pybatfish` to `[[tool.mypy.overrides]]` in `pyproject.toml` with `ignore_missing_imports = true` and commit that change separately.

- [ ] **Step 2: Run the full pytest suite**

Run: `uv run pytest`
Expected: all tests pass. Investigate any regression — most likely candidates are the catalog test (yaml shape) or pre-existing tests inadvertently picking up `BATFISH_DISABLED` from a stale env.

- [ ] **Step 3: Commit any cleanups**

```bash
git add -p
git commit -m "chore: lint cleanups after batfish check rollout"
```

(Skip if there is nothing to commit.)

---

## Task 18: Smoke-test end-to-end against the local stack

**Files:** none (manual verification)

- [ ] **Step 1: Start the stack with the batfish sidecar**

Run: `uv run invoke start`
Expected: `docker compose ps` shows the `batfish` container running alongside the rest.

- [ ] **Step 2: Bootstrap and render artifacts**

Run: `uv run invoke bootstrap`
Expected: artifacts are generated; the Infrahub UI shows `pe-arista-eos`, `pe-cisco-iosxr`, `pe-juniper-junos` artifacts on at least one PE each.

- [ ] **Step 3: Run the check manually**

Run: `uv run invoke batfish-check`
Expected: exit 0 with no `[fileParseStatus]` errors on a green tree. Output contains either "no findings" or only `[bgpSessionCompatibility]` / `[isisEdges]` WARNINGS (which don't fail the check).

- [ ] **Step 4: Negative-control test (optional)**

Edit a template (e.g. add a reference to a route-map that doesn't exist), regenerate artifacts, re-run the check. Confirm an `[undefinedReferences]` ERROR appears and the check fails.

Revert the template change before continuing.

- [ ] **Step 5: Tear down**

Run: `uv run invoke stop`
Expected: containers stop cleanly.

---

## Done

At this point:

- `batfish_backbone` is a registered Infrahub check that runs on every proposed change touching the MPLS topology.
- Unit tests cover the helpers, the check class, and the registration.
- The `batfish/allinone` sidecar runs alongside the rest of the stack and is reachable from the git-agent worker.
- Reviewers see one ERROR per parse failure / undefined reference, one WARNING per BGP/IS-IS asymmetry, and one INFO entry per skipped Nokia device or missing artifact.
