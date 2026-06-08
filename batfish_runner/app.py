"""HTTP wrapper around the Batfish (pybatfish) engine.

Runs as the ``batfish-runner`` sidecar. The Infrahub ``BatfishBackboneCheck``
executes inside the stock task-worker image, which does not ship
``pybatfish``/``pandas``; instead of baking those into the worker, the check
POSTs rendered configs here and this service drives Batfish.

Endpoints:
    GET  /health  -> liveness probe.
    POST /check   -> run the query battery on a set of configs, return findings.

The engine itself (snapshot lifecycle, query battery, finding mapping) is reused
verbatim from ``checks/batfish_helpers.py``; this module only owns the HTTP
contract, the temp snapshot directory, and JSON-safe serialization of the
pandas-derived ``detail`` payloads.

See ``docs/superpowers/specs/2026-06-08-batfish-runner-sidecar-design.md``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from checks.batfish_helpers import Finding, run_snapshot, wait_for_batfish

app = Flask(__name__)


def _jsonable(value: Any) -> Any:
    """Coerce a pandas/numpy-derived value into something ``json`` can encode.

    ``Finding.detail`` comes from ``DataFrame.to_dict()`` and can contain numpy
    scalars, nested structs, and lists. Native types pass through; numpy scalars
    are unwrapped via ``.item()``; anything else falls back to ``str``.

    Args:
        value: Arbitrary value from a finding detail payload.

    Returns:
        A JSON-serializable representation of ``value``.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _jsonable(item())
        except Exception:  # noqa: BLE001 — fall back to str on any unwrap failure
            pass
    return str(value)


def _serialize(findings: list[Finding]) -> list[dict[str, Any]]:
    """Map findings to JSON-safe dicts matching the worker's ``Finding`` fields."""
    return [
        {
            "severity": f.severity,
            "query": f.query,
            "node": f.node,
            "message": f.message,
            "detail": _jsonable(f.detail) if f.detail is not None else None,
        }
        for f in findings
    ]


@app.get("/health")
def health() -> Any:
    """Liveness probe — does not touch Batfish."""
    return jsonify({"status": "ok"})


@app.post("/check")
def check() -> Any:
    """Run the Batfish query battery on the posted configs.

    Request JSON:
        ``network``: Batfish network name.
        ``snapshot``: unique snapshot name for this run.
        ``expected_hosts``: hostnames that should form a full IS-IS mesh.
        ``configs``: mapping of hostname -> rendered config text.

    Returns:
        ``200 {"findings": [...]}`` on success,
        ``400`` on a malformed request,
        ``503`` if the Batfish coordinator is unreachable,
        ``500`` if the engine raises.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    configs = payload.get("configs")
    if not isinstance(configs, dict) or not configs:
        return jsonify({"error": "configs must be a non-empty object"}), 400

    network = payload.get("network", "infrahub-mpls")
    snapshot_name = payload.get("snapshot", "snapshot")
    expected_hosts = set(payload.get("expected_hosts", list(configs)))

    host = os.environ.get("BATFISH_HOST", "batfish")
    port = int(os.environ.get("BATFISH_PORT", "9996"))
    if not wait_for_batfish(host, port=port, timeout_s=60, backoff_s=2):
        return jsonify({"error": f"Batfish service unreachable at {host}:{port}"}), 503

    # Deferred — pybatfish is heavy and only needed once a run actually starts.
    from pybatfish.client.session import Session  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix=f"batfish-{snapshot_name}-") as tmp:
        configs_dir = Path(tmp) / "configs"
        configs_dir.mkdir()
        for hostname, body in configs.items():
            (configs_dir / f"{hostname}.cfg").write_text(body)

        try:
            findings = run_snapshot(
                session=Session(host=host),
                snapshot_dir=Path(tmp),
                network=network,
                snapshot_name=snapshot_name,
                expected_hosts=expected_hosts,
            )
        except Exception as exc:  # noqa: BLE001 — surface any engine failure as 500
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    return jsonify({"findings": _serialize(findings)})


if __name__ == "__main__":
    # Dev server is sufficient: Batfish runs are serialized and this is a demo
    # sidecar. ``threaded`` keeps /health responsive during a long /check.
    app.run(host="0.0.0.0", port=8080, threaded=True)  # noqa: S104
