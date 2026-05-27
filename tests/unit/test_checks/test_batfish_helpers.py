"""Unit tests for batfish helper functions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from checks.batfish_helpers import (
    SUPPORTED_PLATFORMS,
    Finding,
    findings_from_bgp_session_compat,
    findings_from_isis_edges,
    findings_from_parse_status,
    findings_from_parse_warning,
    findings_from_undefined_references,
    run_snapshot,
    wait_for_batfish,
)


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


# ---------------------------------------------------------------------------
# run_snapshot tests
# ---------------------------------------------------------------------------


def _fake_session_factory(answers: dict[str, pd.DataFrame]) -> MagicMock:
    """Build a fake pybatfish Session that returns canned DataFrames per query."""
    session = MagicMock()
    session.q.fileParseStatus.return_value.answer.return_value.frame.return_value = answers.get(
        "fileParseStatus", pd.DataFrame(columns=["File_Name", "Status", "Nodes"])
    )
    session.q.parseWarning.return_value.answer.return_value.frame.return_value = answers.get(
        "parseWarning",
        pd.DataFrame(columns=["Filename", "Line", "Text", "Comment", "Parser_Context"]),
    )
    session.q.undefinedReferences.return_value.answer.return_value.frame.return_value = answers.get(
        "undefinedReferences",
        pd.DataFrame(columns=["File_Name", "Lines", "Type", "Structure_Name", "Context"]),
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
    session.init_snapshot.assert_called_once_with(str(tmp_path), name="snap-1", overwrite=True)
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


# ---------------------------------------------------------------------------
# wait_for_batfish tests
# ---------------------------------------------------------------------------


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
