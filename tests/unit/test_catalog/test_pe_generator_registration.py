"""Guard the PE onboarding generator against a self-referential query.

The generator creates the DcimDevice that ``TopologyPeOnboarding.device`` points
to and adds it to the backbone's ``pes``. If ``queries/service/pe_onboarding.gql``
returned either field, the generator's query group would track its own output
and destabilise inside the proposed-change pipeline, as described in
test_sdwan_generator_registration.py. The other PEs' loopbacks are read through
``generators.common.backbone_pe_loopbacks`` instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

QUERY_PATH = Path("queries/service/pe_onboarding.gql")


def _generators() -> dict[str, dict]:
    cfg = yaml.safe_load(Path(".infrahub.yml").read_text())
    return {g["name"]: g for g in cfg.get("generator_definitions", [])}


def _query_body() -> str:
    """Return the query with its comment lines removed, so the header can name the fields."""
    lines = QUERY_PATH.read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_pe_generator_registered() -> None:
    g = _generators()["generate_pe"]
    assert g["class_name"] == "PeOnboardingGenerator"
    assert g["file_path"] == "generators/generate_pe.py"
    assert g["targets"] == "pe_onboardings"
    assert g["query"] == "pe_onboarding"
    assert g["execute_in_proposed_change"] is False
    assert "generators/common.py" in g["watch"]["files"]
    assert Path(g["file_path"]).exists()


def test_pe_generator_query_not_self_referential() -> None:
    body = _query_body()
    for field in ("device", "pes"):
        assert not re.search(rf"\b{field}\s*{{", body), (
            f"{QUERY_PATH} returns generator-created field '{field}'; this makes the "
            "generator query self-referential and destabilises the proposed-change pipeline"
        )


def test_joining_pe_onboardings_triggers_the_generator() -> None:
    """Without the trigger nothing runs the generator, and every request stays a draft."""
    documents = list(yaml.safe_load_all(Path("objects/events/00_triggers.yml").read_text()))
    rows = [row for doc in documents if doc for row in doc["spec"]["data"]]
    action = next(row for row in rows if row.get("generator") == "generate_pe")
    rule = next(row for row in rows if row.get("action") == action["name"])
    assert rule["group"] == "pe_onboardings"
    assert rule["member_update"] == "added"
