"""Dependency-free shared types for the Batfish check and runner.

This module deliberately has **no** third-party imports (no ``requests``,
``pandas``, or ``pybatfish``). Both sides of the split import from here:

- ``checks/batfish_backbone.py`` runs inside the stock Infrahub task-worker,
  which only ships ``infrahub`` + its deps. It must not transitively import the
  heavy Batfish engine.
- ``batfish_runner/app.py`` and ``checks/batfish_helpers.py`` (the engine) run
  in the ``batfish-runner`` image, which has ``pybatfish``/``pandas``.

Keeping ``Finding`` and ``SUPPORTED_PLATFORMS`` here lets the worker partition
PEs and reconstruct findings from the runner's JSON without pulling in pandas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Platforms Batfish parses well. Nokia SR OS support is experimental and SR
# Linux is unsupported — both are filtered out of the snapshot.
SUPPORTED_PLATFORMS: frozenset[str] = frozenset({"arista_eos", "cisco_iosxr", "juniper_junos"})


@dataclass
class Finding:
    """One result row from a Batfish query, normalized for emission.

    Attributes:
        severity: ``error``, ``warning``, or ``info``.
        query: pybatfish query name that produced the finding.
        node: PE hostname the finding is attributed to, if applicable.
        message: One-line human summary.
        detail: Raw row payload (e.g. the pandas Series as dict) for the
            full message body. ``None`` when the finding isn't row-derived.
    """

    severity: Literal["error", "warning", "info"]
    query: str
    node: str | None
    message: str
    detail: dict[str, Any] | None
