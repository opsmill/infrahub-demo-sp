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


def main() -> int:
    """Trigger every artifact definition; wait for all artifacts to be Ready.

    Returns:
        Exit code (0 if every artifact is Ready before the timeout,
        non-zero if any remain non-Ready when the timeout expires).
    """
    client = InfrahubClientSync()
    definitions = client.all(kind="CoreArtifactDefinition")
    if not definitions:
        print("No CoreArtifactDefinitions registered; nothing to regenerate.")
        return 0

    for definition in definitions:
        _trigger_generate(client, definition.id)
        print(f"queued: {definition.name.value}")

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        artifacts = client.all(kind="CoreArtifact")
        if artifacts and all(a.status.value == "Ready" for a in artifacts):
            print(f"All {len(artifacts)} artifacts Ready.")
            return 0
        time.sleep(POLL_INTERVAL_SECONDS)

    artifacts = client.all(kind="CoreArtifact")
    stuck = [(a.name.value, a.status.value) for a in artifacts if a.status.value != "Ready"]
    print(f"Timed out after {READY_TIMEOUT_SECONDS}s; not Ready: {stuck}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
