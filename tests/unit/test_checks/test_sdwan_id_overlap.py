"""Unit tests for sdwan_id_overlap check."""

from __future__ import annotations

import pytest

from checks.sdwan_id_overlap import SdwanIdOverlapCheck


def _svc_node(name: str, service_id: int | None) -> dict:
    """Build a minimal ServiceSdwan edge node for testing.

    Args:
        name: Service name.
        service_id: Numeric service ID, or None to simulate a null value.

    Returns:
        A dict shaped like a GraphQL edge node.
    """
    sid_field: dict = {"value": service_id} if service_id is not None else {"value": None}
    return {
        "node": {
            "name": {"value": name},
            "service_id": sid_field,
        }
    }


@pytest.mark.asyncio
async def test_no_overlap_passes() -> None:
    """Two SD-WAN services with distinct service_ids produce no errors."""
    data = {
        "ServiceSdwan": {
            "edges": [
                _svc_node("a", 100),
                _svc_node("b", 101),
            ]
        }
    }
    check = SdwanIdOverlapCheck(branch="main")
    await check.validate(data)
    assert check.errors == []


@pytest.mark.asyncio
async def test_duplicate_service_id_fails() -> None:
    """Two SD-WAN services with the same service_id produce one error."""
    data = {
        "ServiceSdwan": {
            "edges": [
                _svc_node("a", 100),
                _svc_node("b", 100),
            ]
        }
    }
    check = SdwanIdOverlapCheck(branch="main")
    await check.validate(data)
    assert len(check.errors) == 1
    msg = check.errors[0]["message"]
    assert "100" in msg
    assert "a" in msg and "b" in msg


@pytest.mark.asyncio
async def test_null_service_id_is_skipped() -> None:
    """Services with a null service_id (pre-allocation) are ignored."""
    data = {
        "ServiceSdwan": {
            "edges": [
                _svc_node("a", None),
                _svc_node("b", 100),
            ]
        }
    }
    check = SdwanIdOverlapCheck(branch="main")
    await check.validate(data)
    assert check.errors == []


@pytest.mark.asyncio
async def test_empty_set_passes() -> None:
    """No SD-WAN services means no errors."""
    data: dict = {"ServiceSdwan": {"edges": []}}
    check = SdwanIdOverlapCheck(branch="main")
    await check.validate(data)
    assert check.errors == []
