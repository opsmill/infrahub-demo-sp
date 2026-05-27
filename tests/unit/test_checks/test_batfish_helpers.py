"""Unit tests for batfish helper functions."""

from __future__ import annotations

from checks.batfish_helpers import SUPPORTED_PLATFORMS, Finding


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
