"""Catalog test: ensure batfish_backbone is registered in .infrahub.yml."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_batfish_query_registered() -> None:
    cfg = yaml.safe_load(Path(".infrahub.yml").read_text())
    queries = {q["name"]: q for q in cfg.get("queries", [])}
    assert "batfish_backbone" in queries
    assert queries["batfish_backbone"]["file_path"] == "queries/validation/batfish_backbone.gql"
    assert Path(queries["batfish_backbone"]["file_path"]).exists()


def test_batfish_check_registered() -> None:
    cfg = yaml.safe_load(Path(".infrahub.yml").read_text())
    checks = {c["name"]: c for c in cfg.get("check_definitions", [])}
    assert "batfish_backbone" in checks
    c = checks["batfish_backbone"]
    assert c["class_name"] == "BatfishBackboneCheck"
    assert c["file_path"] == "checks/batfish_backbone.py"
    assert c["targets"] == "topologies_mpls"
    assert Path(c["file_path"]).exists()
