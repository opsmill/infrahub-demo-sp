"""Helpers for the Batfish backbone check.

Owns the pybatfish coupling, snapshot lifecycle, query wrappers, and the
internal ``Finding`` dataclass. The check class in ``batfish_backbone`` uses
these helpers and maps ``Finding`` instances to Infrahub log entries.
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
