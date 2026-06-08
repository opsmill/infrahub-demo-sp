"""Unit tests for the batfish-runner Flask app.

The pybatfish engine (``run_snapshot`` / ``wait_for_batfish``) is mocked so the
HTTP contract, request validation, error mapping, and JSON-safe serialization
are exercised without a live Batfish coordinator.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from batfish_runner import app as runner_app
from checks.batfish_common import Finding


@pytest.fixture()
def client():  # type: ignore[no-untyped-def]
    runner_app.app.config.update(TESTING=True)
    return runner_app.app.test_client()


def test_health_ok(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_check_returns_serialized_findings(client) -> None:  # type: ignore[no-untyped-def]
    findings = [
        Finding(
            severity="error",
            query="undefinedReferences",
            node="pe1",
            message="undefined route-map FOO",
            detail={"File_Name": "configs/pe1.cfg", "Ref_Name": "FOO"},
        )
    ]

    # The tempdir is deleted when /check returns, so capture its contents and
    # the call args during the run_snapshot call rather than afterward.
    captured: dict[str, object] = {}

    def capture(**kwargs: object) -> list[Finding]:
        snapshot_dir = kwargs["snapshot_dir"]
        captured["configs"] = sorted(
            p.name
            for p in (snapshot_dir / "configs").iterdir()  # type: ignore[operator]
        )
        captured["network"] = kwargs["network"]
        captured["snapshot_name"] = kwargs["snapshot_name"]
        captured["expected_hosts"] = kwargs["expected_hosts"]
        return findings

    with (
        patch("batfish_runner.app.wait_for_batfish", return_value=True),
        patch("pybatfish.client.session.Session"),
        patch("batfish_runner.app.run_snapshot", side_effect=capture),
    ):
        resp = client.post(
            "/check",
            json={
                "network": "infrahub-mpls",
                "snapshot": "mpls-backbone-abc",
                "expected_hosts": ["pe1", "pe2"],
                "configs": {"pe1": "! pe1\n", "pe2": "! pe2\n"},
            },
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["findings"] == [
        {
            "severity": "error",
            "query": "undefinedReferences",
            "node": "pe1",
            "message": "undefined route-map FOO",
            "detail": {"File_Name": "configs/pe1.cfg", "Ref_Name": "FOO"},
        }
    ]
    # The snapshot dir handed to the engine contained the posted configs.
    assert captured["network"] == "infrahub-mpls"
    assert captured["snapshot_name"] == "mpls-backbone-abc"
    assert captured["expected_hosts"] == {"pe1", "pe2"}
    assert captured["configs"] == ["pe1.cfg", "pe2.cfg"]


def test_check_batfish_unreachable_returns_503(client) -> None:  # type: ignore[no-untyped-def]
    with patch("batfish_runner.app.wait_for_batfish", return_value=False):
        resp = client.post("/check", json={"configs": {"pe1": "! pe1\n"}})
    assert resp.status_code == 503
    assert "unreachable" in resp.get_json()["error"]


def test_check_rejects_empty_configs(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.post("/check", json={"configs": {}})
    assert resp.status_code == 400


def test_check_rejects_non_object_body(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.post("/check", json=["not", "an", "object"])
    assert resp.status_code == 400


def test_check_engine_error_returns_500(client) -> None:  # type: ignore[no-untyped-def]
    with (
        patch("batfish_runner.app.wait_for_batfish", return_value=True),
        patch("pybatfish.client.session.Session"),
        patch("batfish_runner.app.run_snapshot", side_effect=RuntimeError("boom")),
    ):
        resp = client.post("/check", json={"configs": {"pe1": "! pe1\n"}})
    assert resp.status_code == 500
    assert "RuntimeError" in resp.get_json()["error"]


def test_jsonable_unwraps_numpy_like_and_falls_back_to_str() -> None:
    class _NumpyLike:
        def item(self) -> int:
            return 42

    class _Opaque:
        def __str__(self) -> str:
            return "opaque"

    out = runner_app._jsonable({"count": _NumpyLike(), "nested": [_Opaque()], "ok": "str", "n": 3})
    assert out == {"count": 42, "nested": ["opaque"], "ok": "str", "n": 3}
