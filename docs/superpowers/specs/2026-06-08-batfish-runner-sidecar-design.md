# Batfish Runner Sidecar — Design

**Status:** Draft
**Date:** 2026-06-08
**Author:** Pete Crocker

## Problem

The `BatfishBackboneCheck` is registered in `.infrahub.yml` and fires during the
proposed-change pipeline, but it silently skips. Its log shows:

```
pybatfish not installed in this environment; check skipped
Check successfully completed
```

Infrahub executes repository checks inside the **`task-worker`** service, which
runs the stock `registry.opsmill.io/opsmill/infrahub` image. That image ships
`infrahub` and its dependencies but not `pybatfish`/`pandas`. The check's
deferred `import pybatfish` raises `ImportError`, hits the guard, and passes
vacuously. Declaring `pybatfish` in `pyproject.toml` only populates the local
`uv` venv (used by `invoke batfish`) and the Streamlit image (built from
`service_catalog/Dockerfile`) — never the task-worker.

So Batfish validation never actually runs during a proposed change.

## Goal

Make the registered Infrahub check produce real Batfish findings during the
proposed-change pipeline, without baking `pybatfish`/`pandas` into the stock
Infrahub task-worker image.

## Approach: Batfish-runner sidecar (Option C)

Keep the task-worker image stock. Move the heavy `pybatfish` engine into a
small dedicated HTTP service (`batfish-runner`) that sits between the worker
and the existing `batfish/allinone` coordinator. The check stays a native
Infrahub check in the worker; it just calls the runner over HTTP instead of
importing `pybatfish` locally.

```
task-worker (stock infrahub image — only needs httpx, already present)
  └─ BatfishBackboneCheck.validate()
        1. GraphQL: backbone + PEs + platforms          (unchanged)
        2. SDK: fetch rendered config artifact per PE    (unchanged)
        3. POST {network, snapshot, expected_hosts, configs} → batfish-runner
        4. receive {findings: [...]}
        5. emit findings as Infrahub logs               (unchanged)

batfish-runner (NEW container: python:3.12-slim + flask + pybatfish + pandas)
  └─ POST /check
        - write configs to <tmp>/configs/<host>.cfg
        - wait_for_batfish(coordinator)
        - run_snapshot(...) -> list[Finding]             (REUSES batfish_helpers)
        - return JSON-safe findings
  └─ GET /health

batfish (existing batfish/allinone sidecar)              (unchanged)
```

### Why this over baking pybatfish into the worker

- Zero risk of dependency conflicts with the Infrahub image's pinned deps
  (numpy/pandas/pydantic).
- The worker image stays exactly as upstream ships it.
- The runner owns one concern and reuses the already-tested
  `checks/batfish_helpers.py` engine verbatim.

Cost: one new small service and an HTTP contract.

## Components

### New files

- `checks/batfish_common.py` — dependency-free shared module holding the
  `Finding` dataclass and `SUPPORTED_PLATFORMS`. Imported by both the worker
  check (which must not pull in `requests`/`pandas`) and the runner.
- `batfish_runner/app.py` — Flask app exposing `GET /health` and `POST /check`.
  Imports `checks.batfish_helpers` (`wait_for_batfish`, `run_snapshot`).
- `batfish_runner/requirements.txt` — `flask`, `pybatfish`, `pandas`,
  `requests`.
- `batfish_runner/Dockerfile` — `python:3.12-slim`, installs requirements;
  source and `checks/` are bind-mounted at runtime (mirrors the Streamlit
  image pattern).
- `tests/unit/test_batfish_runner/test_app.py` — Flask test client, mocked
  `wait_for_batfish` / `run_snapshot`.

### Modified files

- `checks/batfish_helpers.py` — import `Finding` and `SUPPORTED_PLATFORMS`
  from `batfish_common` and re-export them (keeps existing imports working).
- `checks/batfish_backbone.py` — replace the pybatfish/snapshot block with an
  `httpx` POST to `BATFISH_RUNNER_URL`. Import `SUPPORTED_PLATFORMS`/`Finding`
  from `batfish_common`. Keep the `BATFISH_DISABLED=1` short-circuit. Drop the
  `pybatfish`/`run_snapshot`/`wait_for_batfish` imports.
- `docker-compose.override.yml` — add the `batfish-runner` service (build from
  `batfish_runner/`, bind-mount `./batfish_runner` and `./checks:ro`, env
  `BATFISH_HOST=batfish`/`BATFISH_PORT=9996`, `depends_on: [batfish]`, host
  port exposed for `invoke batfish`). Add `BATFISH_RUNNER_URL` to `task-worker`.
- `tests/unit/test_checks/test_batfish_backbone.py` — mock the HTTP call to the
  runner instead of `pybatfish`/`run_snapshot`.

### Unchanged

- `.infrahub.yml` registration (check still `batfish_backbone`, targets
  `topologies_mpls`).
- `checks/batfish_helpers.py` engine logic and `tests/.../test_batfish_helpers.py`.
- `service_catalog/pages/3_Batfish_Check.py` — keeps its own direct pybatfish
  path (it's a dev tool and has pybatfish in its image). Optional future
  follow-up: route it through the runner too.

## HTTP contract

`POST /check`

Request:

```json
{
  "network": "infrahub-mpls",
  "snapshot": "mpls-backbone-ab12cd34",
  "expected_hosts": ["pe1", "pe2"],
  "configs": {"pe1": "<config text>", "pe2": "<config text>"}
}
```

Response `200`:

```json
{"findings": [
  {"severity": "error", "query": "undefinedReferences", "node": "pe1",
   "message": "...", "detail": {"...": "..."}}
]}
```

Response `503` (Batfish coordinator unreachable) / `500` (engine error):

```json
{"error": "Batfish service unreachable at batfish:9996"}
```

`detail` payloads originate from pandas rows; the runner sanitizes them to
JSON-safe types (numpy scalars → native, everything unknown → `str`).

`GET /health` → `200 {"status": "ok"}`.

## Failure model (unchanged at the boundary)

- ERROR findings (`fileParseStatus` fatal, `undefinedReferences`) fail the check.
- WARNING (`bgpSessionCompatibility`, `isisEdges`, partial parse) are
  informational.
- Runner unreachable / non-200 → worker emits one `log_error`
  (`"batfish-runner unreachable at <url>: <reason>"`) so the check fails loudly
  rather than skipping silently. This is the key behavior change: no more silent
  pass.
- `BATFISH_DISABLED=1` still short-circuits to an INFO log + pass (keeps pytest
  and offline runs green).

## Networking

`task-worker`, `batfish-runner`, and `batfish` all share the implicit
`sp-demo_default` compose network, so service-name resolution works
(`batfish-runner:8080`, `batfish:9996`). The runner's host port is exposed so
the host-side `invoke batfish` task can reach it via
`BATFISH_RUNNER_URL=http://localhost:<port>`.

## Testing

- `tests/unit/test_batfish_runner/test_app.py` — `/health` returns ok;
  `/check` with mocked `run_snapshot` returns serialized findings; `/check`
  with `wait_for_batfish` False returns 503; non-JSON-safe `detail` is
  serialized without error.
- `tests/unit/test_checks/test_batfish_backbone.py` — rewritten to mock the
  `httpx` POST: happy path emits no errors; an ERROR finding in the response
  calls `log_error`; a non-200/timeout emits the unreachable `log_error`;
  `BATFISH_DISABLED=1` short-circuits without an HTTP call; Nokia PEs are
  filtered before the POST.
- Existing `test_batfish_helpers.py` and `test_batfish_registration.py`
  unchanged.

## Verification

`uv run invoke init` brings up the full stack (including `batfish-runner`).
Open a proposed change touching the MPLS backbone and confirm the
`batfish_backbone` check reports real findings (or a clean pass) instead of
"pybatfish not installed … check skipped". `uv run invoke batfish` exercises the
same path from the host.

## Rollout

Additive. No schema/transform/generator/check-registration changes. Environments
without `docker-compose.override.yml` (CI lint job) never start the runner; the
check there short-circuits via `BATFISH_DISABLED` or fails closed if pointed at
a missing runner — CI does not run the live path.
