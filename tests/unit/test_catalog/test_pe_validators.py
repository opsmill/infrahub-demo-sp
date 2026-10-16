"""Unit tests for the Add PE form validator and materialisation gate."""

from __future__ import annotations

import pytest

from generators.generate_pe import PLATFORM_GROUP
from service_catalog.utils.validators import PE_PLATFORMS, pe_is_materialised, validate_add_pe_form

BACKBONE = {"pe-01", "pe-02", "pe-07"}
EXISTING = {"pe-01", "pe-02", "pe-07", "ce-trading-lon"}


def _errors(**overrides: object) -> list[str]:
    args: dict = {
        "name": "pe-09",
        "platform": "arista_eos",
        "existing_device_names": EXISTING,
        "uplinks": ["pe-02", "pe-07"],
        "backbone_pes": BACKBONE,
    }
    args.update(overrides)
    return validate_add_pe_form(**args)


def test_happy_path_returns_empty() -> None:
    assert _errors() == []


def test_name_pattern_enforced() -> None:
    assert any("lowercase" in e for e in _errors(name="PE 09"))
    assert any("lowercase" in e for e in _errors(name=""))


def test_existing_device_name_rejected() -> None:
    assert any("already exists" in e for e in _errors(name="pe-02"))


def test_minimum_two_distinct_uplinks() -> None:
    assert any("at least 2" in e for e in _errors(uplinks=["pe-02"]))
    assert any("at least 2" in e for e in _errors(uplinks=["pe-02", "pe-02"]))


def test_self_uplink_rejected() -> None:
    assert any("its own uplink" in e for e in _errors(name="pe-09", uplinks=["pe-09", "pe-02"]))


def test_uplink_outside_backbone_rejected() -> None:
    errors = _errors(uplinks=["pe-02", "pe-99"])
    assert any("Not in the selected backbone: pe-99" in e for e in errors)


@pytest.mark.parametrize("platform", ["cisco_viptela", "versa_flexvnf", "vyos"])
def test_unsupported_platform_rejected(platform: str) -> None:
    assert any("Unsupported PE platform" in error for error in _errors(platform=platform))


def test_catalog_platforms_match_generator_support() -> None:
    assert PE_PLATFORMS == set(PLATFORM_GROUP)


def _request(status: str = "active", device: bool = True) -> dict:
    return {"status": {"value": status}, "device": {"node": {"id": "d9"} if device else None}}


def test_materialised_when_counts_match() -> None:
    assert pe_is_materialised(
        _request(),
        core_interface_count=2,
        ibgp_session_count=8,
        expected_uplinks=2,
        expected_sessions=8,
    )


def test_draft_status_is_not_materialised() -> None:
    assert not pe_is_materialised(
        _request(status="draft"),
        core_interface_count=2,
        ibgp_session_count=8,
        expected_uplinks=2,
        expected_sessions=8,
    )


def test_missing_device_link_is_not_materialised() -> None:
    assert not pe_is_materialised(
        _request(device=False),
        core_interface_count=2,
        ibgp_session_count=8,
        expected_uplinks=2,
        expected_sessions=8,
    )


def test_short_counts_are_not_materialised() -> None:
    assert not pe_is_materialised(
        _request(),
        core_interface_count=1,
        ibgp_session_count=8,
        expected_uplinks=2,
        expected_sessions=8,
    )
    assert not pe_is_materialised(
        _request(),
        core_interface_count=2,
        ibgp_session_count=7,
        expected_uplinks=2,
        expected_sessions=8,
    )


def test_missing_request_is_not_materialised() -> None:
    assert not pe_is_materialised(
        None, core_interface_count=2, ibgp_session_count=8, expected_uplinks=2, expected_sessions=8
    )
