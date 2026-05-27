"""Unit tests for batfish helper functions."""

from __future__ import annotations

import pandas as pd

from checks.batfish_helpers import (
    SUPPORTED_PLATFORMS,
    Finding,
    findings_from_parse_status,
    findings_from_parse_warning,
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
