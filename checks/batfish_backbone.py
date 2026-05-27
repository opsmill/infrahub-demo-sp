"""Batfish-driven validation check for the MPLS backbone.

Runs on every Infrahub proposed change targeting ``topologies_mpls``. For each
backbone, fetches per-PE rendered configs via the Infrahub SDK, filters out
unsupported vendors, loads the configs into a temporary Batfish snapshot, runs
the query battery, and maps findings to Infrahub log entries.

See ``docs/superpowers/specs/2026-05-26-batfish-mpls-ci-validation-design.md``.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from infrahub_sdk.checks import InfrahubCheck
from pybatfish.client.session import Session

from checks.batfish_helpers import (
    SUPPORTED_PLATFORMS,
    Finding,
    run_snapshot,
    wait_for_batfish,
)

logger = logging.getLogger(__name__)

_ARTIFACT_NAME_BY_PLATFORM = {
    "arista_eos": "pe-arista-eos",
    "cisco_iosxr": "pe-cisco-iosxr",
    "juniper_junos": "pe-juniper-junos",
}


class BatfishBackboneCheck(InfrahubCheck):
    """Validate every MPLS backbone with Batfish."""

    query = "batfish_backbone"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Run Batfish queries against each backbone in ``data`` and log findings.

        Args:
            data: Result of the ``batfish_backbone`` GraphQL query, scoped to
                one backbone per invocation by the ``topologies_mpls`` target.
        """
        if os.environ.get("BATFISH_DISABLED") == "1":
            logger.info("Batfish disabled by environment")
            return

        for edge in data.get("TopologyMplsBackbone", {}).get("edges", []):
            await self._validate_backbone(edge["node"])

    async def _validate_backbone(self, backbone: dict[str, Any]) -> None:
        """Validate a single backbone node.

        Args:
            backbone: A single backbone node dict from the GraphQL result.
        """
        backbone_name = backbone["name"]["value"]
        supported_pes, skipped = self._partition_pes(backbone)

        for pe_name, platform_name in skipped:
            logger.info("skipping %s: batfish does not support platform %s", pe_name, platform_name)

        if not supported_pes:
            logger.info("no supported PEs to validate in backbone %s", backbone_name)
            return

        with tempfile.TemporaryDirectory(prefix=f"batfish-{backbone_name}-") as tmp:
            tmp_path = Path(tmp)
            configs_dir = tmp_path / "configs"
            configs_dir.mkdir()

            hosts_in_snapshot: set[str] = set()
            for pe_id, pe_name, platform_name in supported_pes:
                body = await self._fetch_artifact(pe_id=pe_id, platform_name=platform_name)
                if body is None:
                    logger.info("no artifact yet for %s — skipping in snapshot", pe_name)
                    continue
                (configs_dir / f"{pe_name}.cfg").write_text(body)
                hosts_in_snapshot.add(pe_name)

            if not hosts_in_snapshot:
                logger.info("no PE artifacts available for backbone %s", backbone_name)
                return

            host = os.environ.get("BATFISH_HOST", "batfish")
            port = int(os.environ.get("BATFISH_PORT", "9997"))
            if not wait_for_batfish(host, port=port, timeout_s=60, backoff_s=2):
                self.log_error(message=f"Batfish service unreachable at {host}:{port}")
                return

            session = Session(host=host)
            snapshot_name = f"{backbone_name}-{uuid.uuid4().hex[:8]}"
            findings = run_snapshot(
                session=session,
                snapshot_dir=tmp_path,
                network="infrahub-mpls",
                snapshot_name=snapshot_name,
                expected_hosts=hosts_in_snapshot,
            )
            self._emit_findings(findings)

    def _partition_pes(
        self, backbone: dict[str, Any]
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
        """Split PEs into (supported, skipped) based on platform.

        Returns:
            Tuple of (supported, skipped) where supported is a list of
            ``(pe_id, pe_name, platform_name)`` and skipped is ``(pe_name, platform_name)``.
        """
        supported: list[tuple[str, str, str]] = []
        skipped: list[tuple[str, str]] = []
        for pe_edge in backbone.get("pes", {}).get("edges", []):
            pe = pe_edge["node"]
            platform_node = (pe.get("platform") or {}).get("node") or {}
            platform_name = (platform_node.get("name") or {}).get("value") or ""
            pe_name = pe["name"]["value"]
            pe_id = pe["id"]
            if platform_name in SUPPORTED_PLATFORMS:
                supported.append((pe_id, pe_name, platform_name))
            else:
                skipped.append((pe_name, platform_name))
        return supported, skipped

    async def _fetch_artifact(self, pe_id: str, platform_name: str) -> str | None:
        """Fetch the latest rendered config artifact for ``pe_id``.

        Args:
            pe_id: Infrahub node id of the PE device.
            platform_name: Platform name used to choose the artifact definition.

        Returns:
            The artifact body as text, or None if the artifact does not exist.
        """
        artifact_def = _ARTIFACT_NAME_BY_PLATFORM[platform_name]
        try:
            artifact = await self.client.get(
                kind="CoreArtifact",
                object__ids=[pe_id],
                definition__name__value=artifact_def,
            )
        except Exception:  # noqa: BLE001 — SDK raises a variety of "not found" errors
            return None
        storage_id_attr = getattr(artifact, "storage_id", None)
        storage_id = storage_id_attr.value if storage_id_attr is not None else None
        if not storage_id:
            return None
        body = await self.client.object_store.get(identifier=storage_id)
        if isinstance(body, bytes):
            return body.decode("utf-8")
        return str(body)

    def _emit_findings(self, findings: list[Finding]) -> None:
        """Map findings to Infrahub log entries.

        ERROR findings call ``log_error`` (which fails the check). WARNING and
        INFO findings go to the stdlib logger so they appear in check
        execution logs but do not fail the check.
        """
        for f in findings:
            if f.severity == "error":
                self.log_error(message=f"[{f.query}] {f.message}")
            elif f.severity == "warning":
                logger.warning("[%s] %s", f.query, f.message)
            else:
                logger.info("[%s] %s", f.query, f.message)
