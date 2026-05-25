"""Force regeneration of every CoreArtifactDefinition and wait for Ready.

Bootstrap loads objects (which Infrahub auto-dispatches artifact builds
for) before the L3VPN generator runs. The first artifact attempt sees
empty VRF / PE-CE state and lands in ``Error``. Calling this script at
the end of bootstrap — after ``run_generator.py`` has materialized the
data — kicks each artifact definition and waits until every
``CoreArtifact`` row is ``Ready``.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request

from infrahub_sdk import InfrahubClientSync

READY_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 3


def _trigger_generate(client: InfrahubClientSync, definition_id: str) -> None:
    """POST to Infrahub's artifact-generate endpoint for one definition."""
    url = f"{client.address}/api/artifact/generate/{definition_id}"
    request = urllib.request.Request(
        url,
        method="POST",
        headers={"X-INFRAHUB-KEY": os.environ["INFRAHUB_API_TOKEN"]},
    )
    with urllib.request.urlopen(request) as response:
        response.read()


def _storage_ids(client: InfrahubClientSync) -> dict[str, str | None]:
    """Snapshot ``{artifact_id: storage_id}`` across all artifacts.

    storage_id flips to a new UUID when the server re-renders the
    artifact, so it's a reliable trigger-completion signal — Ready
    alone isn't, since artifacts stay Ready across re-render attempts.
    """
    return {a.id: a.storage_id.value for a in client.all(kind="CoreArtifact")}


def main() -> int:
    """Trigger every artifact definition; wait for all artifacts to be Ready.

    Reports which artifacts actually got a new storage_id (re-rendered)
    vs which stayed the same (Infrahub is content-aware and no-ops when
    inputs are unchanged) — that's informational, not a failure.

    Returns:
        Exit code (0 if every artifact is Ready before the timeout,
        non-zero if any are stuck in a non-Ready state).
    """
    client = InfrahubClientSync()
    definitions = client.all(kind="CoreArtifactDefinition")
    if not definitions:
        print("No CoreArtifactDefinitions registered; nothing to regenerate.")
        return 0

    pre_storage = _storage_ids(client)

    for definition in definitions:
        _trigger_generate(client, definition.id)
        print(f"queued: {definition.name.value}")

    # Give the server a moment to move artifacts off Ready before polling.
    time.sleep(POLL_INTERVAL_SECONDS)

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        artifacts = client.all(kind="CoreArtifact")
        if artifacts and all(a.status.value == "Ready" for a in artifacts):
            rerendered = sum(1 for a in artifacts if a.storage_id.value != pre_storage.get(a.id))
            print(
                f"All {len(artifacts)} artifacts Ready "
                f"({rerendered} re-rendered, {len(artifacts) - rerendered} unchanged)."
            )
            return 0
        time.sleep(POLL_INTERVAL_SECONDS)

    artifacts = client.all(kind="CoreArtifact")
    stuck = [(a.name.value, a.status.value) for a in artifacts if a.status.value != "Ready"]
    print(f"Timed out after {READY_TIMEOUT_SECONDS}s; not Ready: {stuck}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
