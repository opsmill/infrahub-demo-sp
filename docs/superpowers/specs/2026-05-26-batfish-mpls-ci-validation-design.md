# Batfish MPLS CI Validation — Design

**Status:** Draft
**Date:** 2026-05-26
**Author:** Pete Crocker

## Summary

Add a Batfish-driven Infrahub check that validates the rendered MPLS backbone
configs on every proposed change. The check runs as part of the existing
proposed-change pipeline, fetches per-PE config artifacts, loads them into a
long-lived `batfish/allinone` sidecar, and surfaces parse, undefined-reference,
and control-plane adjacency findings back to the reviewer.

The motivating problem is the one described in Devendra V's article
*"The tool that made me stop trusting `show running-config`"*: configs render
fine, deploy fine, and still leave broken adjacencies because of dangling
references, AS mismatches, or one-sided neighbors. Static, vendor-aware
analysis catches these before the proposed change is merged.

## Goals

- Validate rendered PE configs on every Infrahub proposed change touching the
  MPLS backbone (schemas, templates, transforms, or data that feeds them).
- Catch the failure modes Batfish is best at: parse errors, undefined
  references, BGP session compatibility, IS-IS adjacency.
- Surface findings in the proposed-change UI alongside the existing
  `l3vpn_overlap` / `backbone_session_count` / etc. checks.
- Be honest about coverage gaps (Nokia SR Linux, Nokia SR OS, LDP) rather
  than pretending they don't exist.

## Non-goals

- LDP / RSVP session validation. Batfish has no first-class query for these
  and approximating them from interface config is a custom check that does
  not need Batfish. Out of scope for this iteration; may revisit as a separate
  Python check.
- VRF leak / route-target import-export validation. The follow-on tier from
  Batfish's L3VPN model has weaker vendor coverage and adds substantial
  complexity. Out of scope.
- Routing simulation (traceroute, BDD reachability). Out of scope; the team
  validated only control-plane adjacency depth.
- GitHub Actions integration. The check runs only as an Infrahub
  proposed-change check. CI continues to do lint + unit tests; it does not
  spin up Infrahub or Batfish.
- Live integration tests in `pytest`. Documented as a manual `invoke` task.

## Vendor coverage

| Platform | Batfish support | Treatment |
|---|---|---|
| Arista EOS | Full | Included in snapshot |
| Cisco IOS-XR | Full | Included in snapshot |
| Juniper Junos | Full | Included in snapshot |
| Nokia SR OS | Limited / experimental | Skipped, INFO finding |
| Nokia SR Linux | None | Skipped, INFO finding |

Skipped devices appear in the check output with the reason. Reviewers see
explicit "PE-NOKIA-1 not validated — Batfish does not support nokia_sros"
findings rather than silent omission.

## Architecture

```
PR opened → Infrahub proposed-change pipeline
            ├─ regenerate artifacts (existing)
            └─ batfish_backbone check (NEW)
                  │
                  │ 1. GraphQL: backbone + PEs + platforms
                  │ 2. SDK: download artifact body per PE
                  │ 3. Write to tmpdir/configs/<hostname>.cfg
                  │ 4. pybatfish.init_snapshot(tmpdir)
                  │ 5. Run query battery
                  │ 6. Map to tiered findings
                  ▼
            batfish/allinone container (sidecar, docker-compose.override.yml)
```

The check targets the `topologies_mpls` group, the same group already used by
the `clab-mpls-topology` artifact. One snapshot is initialized per backbone
topology; the snapshot name combines the backbone name, the proposed-change
id, and a short uuid suffix to avoid collisions across concurrent runs.

The `batfish/allinone` container is a long-lived sidecar in
`docker-compose.override.yml`. The check connects to it on each run via
pybatfish using the compose service name `batfish` as the default host
(overridable with `BATFISH_HOST`).

## Components

### New files

- `checks/batfish_backbone.py` — the check class `BatfishBackboneCheck`
  extending `InfrahubCheck`. Handles data extraction from the GraphQL
  result, artifact fetching, snapshot lifecycle, and result emission.
- `checks/batfish_helpers.py` — snapshot initialization, query wrappers, and
  finding mapping. Kept separate from the check class so the Batfish-specific
  logic is unit-testable in isolation.
- `queries/validation/batfish_backbone.gql` — GraphQL query pulling each
  `TopologyMplsBackbone` along with its PE list and each PE's
  `platform.name.value` and `platform.containerlab_os.value`.
- `tests/unit/test_batfish_backbone.py` — check class tests with mocked
  helpers and SDK.
- `tests/unit/test_batfish_helpers.py` — query mapping tests with mocked
  pybatfish session.
- `tests/catalog/test_batfish_registration.py` — catalog test asserting
  `.infrahub.yml` registration.

### Modified files

- `.infrahub.yml` — register the GraphQL query and the `check_definition`:
  ```yaml
  queries:
    - {name: batfish_backbone, file_path: queries/validation/batfish_backbone.gql}
  check_definitions:
    - name: batfish_backbone
      class_name: BatfishBackboneCheck
      file_path: checks/batfish_backbone.py
      targets: topologies_mpls
  ```
- `docker-compose.override.yml` — add the `batfish` service:
  ```yaml
  services:
    batfish:
      image: batfish/allinone:latest
      restart: unless-stopped
      networks:
        - default
  ```
  No host port binding by default; the check reaches it on the compose
  network.
- `pyproject.toml` — add `pybatfish` to main dependencies (it transitively
  pulls in pandas).

## Data flow

1. Infrahub invokes `BatfishBackboneCheck.validate(data)` once per backbone
   matched by `topologies_mpls`.
2. The check extracts the backbone node and its PEs from the GraphQL result
   passed in via `data`.
3. PEs are partitioned: supported platforms (`arista_eos`, `cisco_iosxr`,
   `juniper_junos`) go to the snapshot; Nokia platforms produce an INFO
   finding and are excluded.
4. For each supported PE, the check uses the Infrahub Python SDK to fetch
   the latest artifact bytes. The artifact name is derived from the PE's
   platform (`pe-arista-eos`, `pe-cisco-iosxr`, `pe-juniper-junos`).
5. Configs are written into a `tempfile.TemporaryDirectory()` with layout
   `<tmp>/configs/<pe_name>.cfg` (Batfish's required snapshot layout).
6. `pybatfish.client.session.Session` connects to `BATFISH_HOST` (default
   `batfish`, port 9997). Network name is `infrahub-mpls`. Snapshot name is
   `f"{backbone.name}-{proposed_change_id}-{uuid4().hex[:8]}"`.
7. Query battery runs (see Query battery).
8. Results are flattened into `CheckResult` objects with severity per the
   Failure model.
9. Snapshot is torn down via `session.delete_snapshot()` in a `finally` block.

## Query battery

Five pybatfish queries, each wrapped in a function in `batfish_helpers.py`
returning `list[Finding]`:

| Query | What it catches | Severity |
|---|---|---|
| `fileParseStatus` | Configs that didn't parse, or parsed only partially (`PARTIALLY_UNRECOGNIZED`, `FAILED`). | ERROR |
| `parseWarning` | Specific unrecognized lines within configs that did parse. Includes file and line in the detail. | ERROR |
| `undefinedReferences` | Route-map / community-list / prefix-list / ACL referenced but not defined. | ERROR |
| `bgpSessionCompatibility` | BGP neighbor classification: anything other than `UNIQUE_MATCH` (e.g. `HALF_OPEN`, `NO_MATCH_FOUND`, AS mismatches). | WARNING |
| `isisEdges` | Inferred IS-IS adjacencies. Compared against the expected PE-to-PE mesh from the topology data; missing edges produce findings. | WARNING |

## Failure model

Tiered:

- **ERROR** — `fileParseStatus`, `parseWarning`, `undefinedReferences`.
  These mean the config is wrong; the check fails (blocks the proposed
  change).
- **WARNING** — `bgpSessionCompatibility`, `isisEdges`. These may be
  intentional during a partial rollout (e.g. adding a PE one device at a
  time). They appear in the conclusion message but don't fail the check.
- **INFO** — Skipped Nokia devices, missing artifacts, "no supported PEs
  to validate". Informational only.

The check passes overall iff there are zero ERROR-severity findings.

## Finding shape

Internal dataclass used by the helpers module before mapping to
`CheckResult`:

```python
@dataclass
class Finding:
    severity: Literal["error", "warning", "info"]
    query: str            # e.g. "bgpSessionCompatibility"
    node: str | None      # PE hostname if applicable
    message: str          # one-line summary
    detail: dict | None   # raw row from the DataFrame, for the message body
```

Each Finding maps to one Infrahub `CheckResult`. The aggregated summary
(counts per severity per query) is included in the overall check conclusion
message.

## Error handling

- **Batfish unreachable.** `requests.exceptions.ConnectionError` and pybatfish's
  `BatfishException` are caught at session-init time. The check emits a
  single ERROR finding `"Batfish service unreachable at {host}:{port}"` and
  returns without attempting queries.
- **Disabled via env.** `BATFISH_DISABLED=1` short-circuits the check before
  any network call, emits one INFO finding `"Batfish disabled by environment"`,
  and passes. Keeps `uv run pytest` working without the sidecar.
- **Artifact missing.** A PE without a rendered artifact (newly added, generator
  hasn't run) produces a WARNING finding `"no artifact yet — skipping in
  snapshot"` and is excluded from the snapshot. Other PEs still validate.
- **Empty snapshot.** If every PE is Nokia-skipped or artifact-missing, snapshot
  init is skipped entirely; the check passes with one INFO finding
  `"no supported PEs to validate"`.
- **Snapshot name collisions.** Names include the proposed-change id plus a
  uuid4 suffix. Cleanup runs in a `finally` block.
- **Cold start.** The check waits up to 60s for `GET /api/health` against the
  Batfish host before issuing queries (2s backoff, max 30 attempts). Init
  failures past that window are reported as ERROR.
- **Unexpected pybatfish exception.** Caught at the query-battery boundary;
  the run aborts with an ERROR finding containing the exception class and a
  truncated message. Snapshot cleanup still runs.

## Logging

- All Batfish interactions log at DEBUG with snapshot name and query name in
  the context.
- Findings log at INFO with severity, query, and node.
- Config text and snapshot bodies are never logged. The check identifies
  configs by PE hostname only.

## Testing

### Unit tests

`tests/unit/test_batfish_helpers.py` — mocks `pybatfish.client.session.Session`
and exercises mapping logic in isolation. Synthetic DataFrames are constructed
to assert the produced `Finding` list:

- `fileParseStatus` DataFrame with one PASSED and one FAILED row → exactly
  one ERROR finding, attributed to the failed file.
- `bgpSessionCompatibility` DataFrame with `UNIQUE_MATCH`, `HALF_OPEN`,
  `NO_MATCH_FOUND` rows → one WARNING per non-unique row, with correct node.
- `undefinedReferences` empty frame → no findings.
- `parseWarning` populated → ERROR findings with line numbers in the detail
  dict.
- `isisEdges` with a missing expected edge → WARNING finding.

`tests/unit/test_batfish_backbone.py` — mocks the helpers module and the
Infrahub SDK artifact fetch. Verifies:

- Nokia PEs are filtered before snapshot init.
- Missing artifacts produce WARNING findings and don't abort the run.
- Empty supported-PE set short-circuits with the INFO finding.
- `BATFISH_DISABLED=1` short-circuits without contacting Batfish.
- Snapshot is deleted in a `finally` block even when queries raise.

### Catalog tests

`tests/catalog/test_batfish_registration.py` — asserts `.infrahub.yml` registers
the GraphQL query and `check_definition` with the expected names and that
the file paths resolve.

### Integration tests

Not wired into `pytest`. A manual procedure is documented: with the local
stack running (`uv run invoke start`), `uv run invoke batfish-check` (new task)
runs the check end-to-end against the running Infrahub. CI does not exercise
the live Batfish path.

### Coverage targets

- `checks/batfish_backbone.py` — 70%
- `checks/batfish_helpers.py` — 80%

## Open questions / future work

- **LDP coverage.** If reviewers find the missing LDP signal painful, a
  follow-up custom check could compare interface-level MPLS knobs across
  links without involving Batfish. Tracked as future work.
- **Per-query check granularity.** If the single composite check makes it
  hard to triage failures in the UI, split into per-query `check_definitions`
  sharing a cached snapshot id. Not done now to keep wiring small.
- **Multiple backbones.** The design assumes the `topologies_mpls` target
  group may contain more than one backbone, each producing its own snapshot
  and check result. Tested in unit tests via mocked multi-backbone data.

## Compatibility & rollout

The check is additive. No existing schemas, transforms, generators, or
checks change. The `batfish` container is added behind
`docker-compose.override.yml`, so `uv run invoke start` brings it up
automatically; environments without the override file (CI lint job) never
contact Batfish.

The first proposed change after this lands will exercise the cold-start path;
subsequent runs reuse the same warm sidecar.
