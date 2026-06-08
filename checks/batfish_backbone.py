"""Batfish-driven validation check for the MPLS backbone.

Runs on every Infrahub proposed change targeting ``topologies_mpls``. For each
backbone, fetches per-PE rendered configs via the Infrahub SDK, filters out
unsupported vendors, and POSTs them to the ``batfish-runner`` sidecar, which
owns the ``pybatfish`` engine. The runner loads a temporary snapshot, runs the
query battery, and returns findings as JSON; this check maps them to Infrahub
log entries.

The split exists because Infrahub executes checks inside the stock
``task-worker`` image, which does not ship ``pybatfish``/``pandas``. Rather than
bake those heavy deps into the worker, the engine lives in ``batfish_runner``
and is reached over HTTP.

See ``docs/superpowers/specs/2026-06-08-batfish-runner-sidecar-design.md``.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
from infrahub_sdk.checks import InfrahubCheck
from infrahub_sdk.exceptions import NodeNotFoundError

from .batfish_common import SUPPORTED_PLATFORMS, Finding

_ARTIFACT_NAME_BY_PLATFORM = {
    "arista_eos": "pe-arista-eos",
    "cisco_iosxr": "pe-cisco-iosxr",
    "juniper_junos": "pe-juniper-junos",
}

# Batfish network name shared across snapshots (matches the runner default).
_NETWORK = "infrahub-mpls"

# How long to wait on the runner. Snapshot init + the query battery can take
# 30-60s on a cold Batfish; the runner also waits internally for the
# coordinator. Keep the client timeout generous so a slow-but-working run
# isn't misreported as unreachable.
_RUNNER_TIMEOUT_S = 180.0


class BatfishBackboneCheck(InfrahubCheck):
    """Validate every MPLS backbone with Batfish (via the runner sidecar)."""

    query = "batfish_backbone"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Run Batfish queries against each backbone in ``data`` and log findings.

        Args:
            data: Result of the ``batfish_backbone`` GraphQL query, scoped to
                one backbone per invocation by the ``topologies_mpls`` target.
        """
        if os.environ.get("BATFISH_DISABLED") == "1":
            self.log_info(message="Batfish disabled by environment")
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
            self.log_info(message=f"[skipped] {pe_name}: batfish does not support {platform_name}")

        if not supported_pes:
            self.log_info(message=f"no supported PEs to validate in backbone {backbone_name}")
            return

        configs: dict[str, str] = {}
        for pe_id, pe_name, platform_name in supported_pes:
            body = await self._fetch_artifact(pe_id=pe_id, platform_name=platform_name)
            if body is None:
                self.log_info(message=f"[skipped] no artifact yet for {pe_name}")
                continue
            configs[pe_name] = body

        if not configs:
            self.log_info(message=f"no PE artifacts available for backbone {backbone_name}")
            return

        snapshot_name = f"{backbone_name}-{uuid.uuid4().hex[:8]}"
        findings = await self._run_via_runner(
            snapshot_name=snapshot_name,
            expected_hosts=sorted(configs),
            configs=configs,
        )
        if findings is not None:
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
                name__value=artifact_def,
            )
        except NodeNotFoundError:
            return None
        storage_id_attr = getattr(artifact, "storage_id", None)
        storage_id = storage_id_attr.value if storage_id_attr is not None else None
        if not storage_id:
            return None
        body: str = await self.client.object_store.get(identifier=storage_id)
        return body

    async def _run_via_runner(
        self,
        snapshot_name: str,
        expected_hosts: list[str],
        configs: dict[str, str],
    ) -> list[Finding] | None:
        """POST configs to the batfish-runner and return the parsed findings.

        On any transport or service error the check fails loudly with a single
        ``log_error`` and ``None`` is returned (so nothing is emitted twice).
        This is deliberate: the previous behavior silently passed when the
        engine was unavailable, which hid the fact that Batfish never ran.

        Args:
            snapshot_name: Unique snapshot name for this run.
            expected_hosts: PE hostnames that should form a full IS-IS mesh.
            configs: Mapping of PE hostname to rendered config text.

        Returns:
            List of findings on success, or ``None`` if the runner could not be
            reached or returned an error (an error is already logged).
        """
        url = os.environ.get("BATFISH_RUNNER_URL", "http://batfish-runner:8080").rstrip("/")
        payload = {
            "network": _NETWORK,
            "snapshot": snapshot_name,
            "expected_hosts": expected_hosts,
            "configs": configs,
        }
        try:
            async with httpx.AsyncClient(timeout=_RUNNER_TIMEOUT_S) as client:
                response = await client.post(f"{url}/check", json=payload)
        except httpx.HTTPError as exc:
            self.log_error(message=f"batfish-runner unreachable at {url}: {exc}")
            return None

        if response.status_code != httpx.codes.OK:
            detail = self._error_detail(response)
            self.log_error(message=f"batfish-runner error ({response.status_code}): {detail}")
            return None

        body = response.json()
        return [Finding(**row) for row in body.get("findings", [])]

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """Extract a human-readable error message from a non-200 runner response."""
        try:
            return str(response.json().get("error", response.text))
        except ValueError:
            return response.text

    def _emit_findings(self, findings: list[Finding]) -> None:
        """Map findings to Infrahub log entries.

        ERROR findings call ``log_error`` (which fails the check). WARNING and INFO
        findings call ``log_info`` so they appear in the proposed-change UI but
        don't fail the check. Warnings carry a [WARN] prefix.
        """
        for f in findings:
            if f.severity == "error":
                self.log_error(message=f"[{f.query}] {f.message}")
            elif f.severity == "warning":
                self.log_info(message=f"[WARN][{f.query}] {f.message}")
            else:
                self.log_info(message=f"[{f.query}] {f.message}")
