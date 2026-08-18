"""Guard the service generators against self-referential queries.

A generator query must return only the *inputs* it reads, never the objects
the generator *creates*. If a generator's query returns its own outputs (e.g.
``sdwan_edge``/``lan_address`` for SD-WAN, ``vrf``/``pe_interface``/
``pe_address``/``ce_address`` for L3VPN), the generator's internal query-group
bookkeeping (``collect_data(update_group=True)``) tracks freshly-created nodes
and destabilises inside the proposed-change pipeline — ``CoreGraphQLQueryGroup``
upsert raises ``NodeNotFound`` and the branch is wiped, so no config artifact
survives. The generators derive idempotency from live relationships instead
(matching the infrahub-demo-dc generator pattern).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _generators() -> dict[str, dict]:
    cfg = yaml.safe_load(Path(".infrahub.yml").read_text())
    return {g["name"]: g for g in cfg.get("generator_definitions", [])}


def test_sdwan_generator_registered() -> None:
    g = _generators()["generate_sdwan"]
    assert g["class_name"] == "SdwanGenerator"
    assert g["file_path"] == "generators/generate_sdwan.py"
    assert g["targets"] == "sdwans"
    assert Path(g["file_path"]).exists()


@pytest.mark.parametrize(
    ("query_path", "forbidden_fields"),
    [
        ("queries/service/sdwan.gql", ["sdwan_edge", "lan_address"]),
        ("queries/service/l3vpn.gql", ["vrf", "pe_interface", "pe_address", "ce_address"]),
    ],
)
def test_generator_query_not_self_referential(query_path: str, forbidden_fields: list[str]) -> None:
    query = Path(query_path).read_text()
    for field in forbidden_fields:
        assert f"{field} " not in query and f"{field}{{" not in query, (
            f"{query_path} returns generator-created field '{field}'; this makes the "
            "generator query self-referential and destabilises the proposed-change pipeline"
        )
