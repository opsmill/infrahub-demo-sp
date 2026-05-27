"""Helpers for the Batfish backbone check.

Owns the pybatfish coupling, snapshot lifecycle, query wrappers, and the
internal ``Finding`` dataclass. The check class in ``batfish_backbone`` uses
these helpers and maps ``Finding`` instances to Infrahub log entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd

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


_PARSE_OK = "PASSED"


def _node_from_row(row: pd.Series[Any], file_name: str) -> str:
    """Pull the first node from a parse-status row, or fall back to the filename stem.

    Args:
        row: A single row from the fileParseStatus DataFrame.
        file_name: The ``File_Name`` value for the row, used as a fallback.

    Returns:
        The first node name, or the stem of the config filename when Nodes is empty.
    """
    nodes = row.get("Nodes") or []
    if isinstance(nodes, list) and nodes:
        return str(nodes[0])
    return Path(file_name).stem


def findings_from_parse_status(df: pd.DataFrame) -> list[Finding]:
    """Map a pybatfish ``fileParseStatus`` answer into ``Finding`` rows.

    Args:
        df: DataFrame with at least ``File_Name``, ``Status``, ``Nodes`` columns.

    Returns:
        One ``Finding`` per non-PASSED row.
    """
    findings: list[Finding] = []
    for _, row in df.iterrows():
        status = str(row["Status"])
        if status == _PARSE_OK:
            continue
        file_name = str(row["File_Name"])
        node = _node_from_row(row, file_name)
        findings.append(
            Finding(
                severity="error",
                query="fileParseStatus",
                node=node,
                message=f"config {file_name} parse status: {status}",
                detail=row.to_dict(),
            )
        )
    return findings


def findings_from_parse_warning(df: pd.DataFrame) -> list[Finding]:
    """Map a pybatfish ``parseWarning`` answer into ``Finding`` rows.

    Args:
        df: DataFrame with ``Filename``, ``Line``, ``Text``, ``Comment`` columns.

    Returns:
        One ``Finding`` per row, all severity ERROR.
    """
    findings: list[Finding] = []
    for _, row in df.iterrows():
        file_name = str(row["Filename"])
        node = Path(file_name).stem
        line = row["Line"]
        findings.append(
            Finding(
                severity="error",
                query="parseWarning",
                node=node,
                message=f"parse warning in {file_name} line {line}: {row['Comment']}",
                detail=row.to_dict(),
            )
        )
    return findings


def findings_from_undefined_references(df: pd.DataFrame) -> list[Finding]:
    """Map a pybatfish ``undefinedReferences`` answer into ``Finding`` rows.

    Args:
        df: DataFrame with ``File_Name``, ``Lines``, ``Type``, ``Structure_Name`` columns.

    Returns:
        One ``Finding`` per row, all severity ERROR.
    """
    findings: list[Finding] = []
    for _, row in df.iterrows():
        file_name = str(row["File_Name"])
        node = Path(file_name).stem
        findings.append(
            Finding(
                severity="error",
                query="undefinedReferences",
                node=node,
                message=(
                    f"undefined {row['Type']} '{row['Structure_Name']}' "
                    f"referenced in {file_name}"
                ),
                detail=row.to_dict(),
            )
        )
    return findings
