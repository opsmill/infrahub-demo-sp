"""Pins the natural key of RoutingBGPSession.

The key is load-bearing in two directions and neither is visible from one file.
``infrahubctl object load`` upserts on the human-friendly ID, so a missing or
wrong key silently duplicates all 68 seeded sessions on every ``invoke
bootstrap``; and the uniqueness constraint decides whether two devices may each
carry a session with the same description, which they must be able to do.

The precondition — ``device`` being mandatory — lives in the RoutingProtocol
generic, one file away, which is exactly why it is asserted here too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas"
BGP_SCHEMA = SCHEMA_DIR / "extensions" / "routing_bgp" / "bgp.yml"
ROUTING_SCHEMA = SCHEMA_DIR / "extensions" / "routing" / "routing.yml"


def _node(path: Path, name: str, section: str = "nodes") -> dict[str, Any]:
    """Return one node or generic definition from a schema file.

    Args:
        path: Schema YAML file to read.
        name: Value of the definition's ``name`` key.
        section: ``nodes`` or ``generics``.

    Returns:
        The definition mapping.
    """
    schema = yaml.safe_load(path.read_text(encoding="utf-8"))
    for definition in schema.get(section, []):
        if definition["name"] == name:
            return dict(definition)
    pytest.fail(f"{section[:-1]} {name} not found in {path}")


def _relationship(definition: dict[str, Any], name: str) -> dict[str, Any]:
    """Return one relationship of a node/generic definition.

    Args:
        definition: A node or generic definition mapping.
        name: Relationship name.

    Returns:
        The relationship mapping.
    """
    for relationship in definition.get("relationships", []):
        if relationship["name"] == name:
            return dict(relationship)
    pytest.fail(f"relationship {name} not found on {definition['name']}")


def test_session_key_is_scoped_by_device() -> None:
    """The key is [device, description], so two routers may share a description.

    Keyed on the description alone, the first device to use a description
    reserved it globally and another device's session was rejected rather than
    inserted.
    """
    session = _node(BGP_SCHEMA, "BGPSession")
    assert session["human_friendly_id"] == ["device__name__value", "description__value"]
    assert session["uniqueness_constraints"] == [["device", "description__value"]]


def test_session_key_components_are_mandatory() -> None:
    """Neither half of the key may be null, or the key is not a key.

    Both halves come from the RoutingProtocol generic, one file away, which is
    why they are asserted here: a nullable either would collapse every session
    lacking it into a single key slot.
    """
    generic = _node(ROUTING_SCHEMA, "Protocol", section="generics")

    device = _relationship(generic, "device")
    assert device["optional"] is False
    assert device["cardinality"] == "one"
    assert device["kind"] == "Parent"

    description = next(a for a in generic["attributes"] if a["name"] == "description")
    assert description["optional"] is False


def test_session_does_not_redeclare_the_inherited_device_relationship() -> None:
    """BGPSession must not restate `device`, or the schema fails to load.

    Redeclaring an inherited relationship does not override it — the server
    merges both and rejects the node with "Only one relationship of type parent
    is allowed, but all the following are of type parent: ['device', 'device']".
    Verified against a live Infrahub 1.10.6. The declaration this node used to
    carry (`optional: true`, no explicit kind) slipped past that check only
    because its kind defaulted to Generic, which quietly weakened the generic's
    mandatory parent — so restoring it "properly" is the tempting wrong move
    this test exists to stop.
    """
    session = _node(BGP_SCHEMA, "BGPSession")
    names = [r["name"] for r in session.get("relationships", [])]
    assert "device" not in names, (
        "`device` is inherited from RoutingProtocol as a mandatory Parent; "
        "declaring it here duplicates the parent relationship and breaks "
        "`infrahubctl schema load`"
    )
