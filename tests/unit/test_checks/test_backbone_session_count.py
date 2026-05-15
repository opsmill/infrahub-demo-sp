"""Unit test for backbone_session_count check."""

from __future__ import annotations

import pytest

from checks.backbone_session_count import BackboneSessionCountCheck


def _pe(name: str) -> dict:
    return {"node": {"name": {"value": name}}}


def _session(device: str) -> dict:
    return {"node": {"device": {"node": {"name": {"value": device}}}}}


@pytest.mark.asyncio
async def test_full_mesh_passes() -> None:
    """4 PEs × 3 sessions each = full mesh, no errors."""
    data = {
        "DcimDevice": {"count": 4, "edges": [_pe("p1"), _pe("p2"), _pe("p3"), _pe("p4")]},
        "RoutingBGPSession": {"edges": (
            [_session("p1")] * 3 + [_session("p2")] * 3
            + [_session("p3")] * 3 + [_session("p4")] * 3
        )},
    }
    assert await BackboneSessionCountCheck.__new__(BackboneSessionCountCheck).validate(data) == []


@pytest.mark.asyncio
async def test_missing_session_fails() -> None:
    """One PE has 2 instead of 3 sessions → fails."""
    data = {
        "DcimDevice": {"count": 4, "edges": [_pe("p1"), _pe("p2"), _pe("p3"), _pe("p4")]},
        "RoutingBGPSession": {"edges": (
            [_session("p1")] * 2 + [_session("p2")] * 3
            + [_session("p3")] * 3 + [_session("p4")] * 3
        )},
    }
    errors = await BackboneSessionCountCheck.__new__(BackboneSessionCountCheck).validate(data)
    assert errors and "p1" in errors[0] and "expected 3" in errors[0]
