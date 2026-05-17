"""Unit tests for sdwan_site_subnet check."""

from __future__ import annotations

import pytest

from checks.sdwan_site_subnet import SdwanSiteSubnetCheck


def _site(name: str, prefix: str | None) -> dict:
    """Build a minimal ServiceSdwanSite edge node for testing.

    Args:
        name: Site name.
        prefix: LAN subnet CIDR string, or None for no subnet relationship.

    Returns:
        A dict shaped like a GraphQL edge node.
    """
    lan_subnet = {"node": {"prefix": {"value": prefix}}} if prefix else {"node": None}
    return {
        "node": {
            "name": {"value": name},
            "lan_subnet": lan_subnet,
        }
    }


def _svc(name: str, sites: list[dict]) -> dict:
    """Build a minimal ServiceSdwan edge node with nested sites.

    Args:
        name: Service name.
        sites: List of site edge dicts built by :func:`_site`.

    Returns:
        A dict shaped like a GraphQL edge node.
    """
    return {"node": {"name": {"value": name}, "sites": {"edges": sites}}}


@pytest.mark.asyncio
async def test_disjoint_subnets_pass() -> None:
    """Sites with non-overlapping subnets produce no errors."""
    data = {
        "ServiceSdwan": {
            "edges": [
                _svc(
                    "acme",
                    [
                        _site("london", "10.250.10.0/24"),
                        _site("paris", "10.250.20.0/24"),
                    ],
                )
            ]
        }
    }
    check = SdwanSiteSubnetCheck(branch="main")
    await check.validate(data)
    assert check.errors == []


@pytest.mark.asyncio
async def test_overlapping_subnets_fail() -> None:
    """A supernet/subnet pair within one service triggers one error."""
    data = {
        "ServiceSdwan": {
            "edges": [
                _svc(
                    "acme",
                    [
                        _site("hub", "10.250.0.0/16"),
                        _site("spoke", "10.250.10.0/24"),
                    ],
                )
            ]
        }
    }
    check = SdwanSiteSubnetCheck(branch="main")
    await check.validate(data)
    assert len(check.errors) == 1
    msg = check.errors[0]["message"]
    assert "acme" in msg
    assert "hub" in msg and "spoke" in msg


@pytest.mark.asyncio
async def test_overlap_only_within_service() -> None:
    """Overlap between two different SD-WAN services is allowed."""
    data = {
        "ServiceSdwan": {
            "edges": [
                _svc("a", [_site("s1", "10.250.10.0/24")]),
                _svc("b", [_site("s2", "10.250.10.0/24")]),
            ]
        }
    }
    check = SdwanSiteSubnetCheck(branch="main")
    await check.validate(data)
    assert check.errors == []


@pytest.mark.asyncio
async def test_null_subnet_is_skipped() -> None:
    """Sites with no lan_subnet relationship are ignored."""
    data = {
        "ServiceSdwan": {
            "edges": [
                _svc(
                    "acme",
                    [
                        _site("hub", None),
                        _site("spoke", "10.250.10.0/24"),
                    ],
                )
            ]
        }
    }
    check = SdwanSiteSubnetCheck(branch="main")
    await check.validate(data)
    assert check.errors == []
