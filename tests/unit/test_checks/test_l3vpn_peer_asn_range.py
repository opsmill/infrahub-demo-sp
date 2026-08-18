"""Unit tests for the l3vpn_peer_asn_range check."""

from __future__ import annotations

from typing import Any

import pytest

from checks.l3vpn_peer_asn_range import L3VpnPeerAsnRangeCheck


def _data(
    sites: list[tuple[str, int | None]],
    *,
    start: int | None = 65100,
    end: int | None = 65199,
    pool_name: str = "customer_asn_pool",
    backbone_asn: int | None = 65000,
) -> dict[str, Any]:
    """Build a query result holding one VPN and the customer ASN pool.

    Args:
        sites: List of (site_name, bgp_peer_asn) tuples; ``None`` means unset.
        start: Pool ``start_range``, or ``None`` to leave it unset.
        end: Pool ``end_range``, or ``None`` to leave it unset.
        pool_name: Name to report the pool under.
        backbone_asn: The provider AS, or ``None`` to omit the backbone entirely.

    Returns:
        A dict shaped like the ``l3vpn_peer_asn_range`` GraphQL result.
    """
    backbone = (
        {
            "edges": [
                {
                    "node": {
                        "name": {"value": "mpls-backbone-1"},
                        "asn": {"node": {"asn": {"value": backbone_asn}}},
                    }
                }
            ]
        }
        if backbone_asn is not None
        else {"edges": []}
    )
    return {
        "TopologyMplsBackbone": backbone,
        "CoreNumberPool": {
            "edges": [
                {
                    "node": {
                        "name": {"value": pool_name},
                        "start_range": {"value": start},
                        "end_range": {"value": end},
                    }
                }
            ]
        },
        "ServiceL3Vpn": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "trading-floor-vpn"},
                        "sites": {
                            "edges": [
                                {
                                    "node": {
                                        "name": {"value": name},
                                        "bgp_peer_asn": {"value": asn},
                                    }
                                }
                                for name, asn in sites
                            ]
                        },
                    }
                }
            ]
        },
    }


@pytest.mark.asyncio
async def test_override_outside_pool_range_passes() -> None:
    """An override below the pool range cannot collide with a pool allocation."""
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("lon", 65001)]))
    assert check.errors == []


@pytest.mark.asyncio
async def test_override_inside_pool_range_fails() -> None:
    """An override inside the pool range is reported with both bounds named."""
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("lon", 65101)]))
    assert len(check.errors) == 1
    message = check.errors[0]["message"]
    assert "65101" in message
    assert "65100-65199" in message


@pytest.mark.asyncio
async def test_unset_override_passes() -> None:
    """A site with no override takes the pool-allocated AS and is never flagged."""
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("lon", None)]))
    assert check.errors == []


@pytest.mark.asyncio
async def test_pool_bounds_are_inclusive() -> None:
    """Both range endpoints are pool-issuable, so both must be rejected."""
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("first", 65100), ("last", 65199)]))
    assert len(check.errors) == 2


@pytest.mark.asyncio
async def test_just_outside_pool_bounds_passes() -> None:
    """The numbers either side of the range are outside it and stay allowed."""
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("below", 65099), ("above", 65200)]))
    assert check.errors == []


@pytest.mark.asyncio
async def test_missing_pool_reports_rather_than_passing() -> None:
    """A renamed or missing pool must fail loudly, not silently enforce nothing.

    Passing here meant the day the pool moved was the day the guarantee
    disappeared, with a green check to say otherwise.
    """
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("lon", 65101)], pool_name="some_other_pool"))
    assert len(check.errors) == 1
    assert "customer_asn_pool" in str(check.errors[0])


@pytest.mark.asyncio
async def test_pool_without_bounds_reports_rather_than_passing() -> None:
    """A pool missing a bound yields no range, which is equally unenforceable."""
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("lon", 65101)], end=None))
    assert len(check.errors) == 1


@pytest.mark.asyncio
async def test_seeded_isp_asn_stays_outside_the_range() -> None:
    """The isp dataset's peer AS is outside the pool range and must pass.

    objects/datasets/isp/80_l3vpn.yml seeds 65001 precisely to stay clear of
    customer_asn_pool; this pins that intent so a future edit back into the
    range fails here rather than at generator runtime.
    """
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("isp-site", 65001)]))
    assert check.errors == []


@pytest.mark.asyncio
async def test_backbone_asn_override_is_rejected() -> None:
    """65000 is the obvious provider-AS guess and must never be adopted.

    The generator would take Backbone-AS as the customer side of an eBGP
    session — the row every PE's device ASN and the whole iBGP mesh point at.
    """
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("lon", 65000)]))
    assert len(check.errors) == 1
    assert "backbone" in str(check.errors[0]).lower()


@pytest.mark.asyncio
async def test_backbone_rejection_does_not_depend_on_the_pool_range() -> None:
    """The backbone AS sits outside the pool, so the range test cannot catch it."""
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("lon", 65000)], start=65100, end=65199))
    assert len(check.errors) == 1


@pytest.mark.asyncio
async def test_no_backbone_in_result_still_checks_the_pool_range() -> None:
    """An absent backbone must not suppress the range check."""
    check = L3VpnPeerAsnRangeCheck(branch="main")
    await check.validate(_data([("lon", 65101)], backbone_asn=None))
    assert len(check.errors) == 1
    assert "range" in str(check.errors[0])
