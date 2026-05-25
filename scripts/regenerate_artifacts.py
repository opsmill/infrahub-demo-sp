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


def _delete_existing_artifacts(client: InfrahubClientSync, definition_id: str) -> int:
    """Delete every ``CoreArtifact`` row belonging to ``definition_id``.

    Infrahub's artifact-generate endpoint dedups against existing artifacts
    by their effective inputs — if the schema data hasn't changed, the
    endpoint no-ops even when the *transform template* did change on disk.
    Deleting the existing rows first forces a fresh render against the
    current template every time.
    """
    arts = client.filters(kind="CoreArtifact", definition__ids=[definition_id])
    for a in arts:
        client.execute_graphql(
            "mutation D($id: String!) { CoreArtifactDelete(data: {id: $id}) { ok } }",
            variables={"id": a.id},
        )
    return len(arts)


def main() -> int:
    """Delete + re-trigger every artifact, then wait for all to be Ready.

    Infrahub's artifact-generate endpoint dedups by effective inputs:
    if schema data hasn't changed, it no-ops even when the *template*
    has changed on disk. Delete-then-regenerate is the only reliable
    way to pick up template-only changes.

    Returns:
        Exit code (0 if every artifact is Ready before the timeout,
        non-zero if any are stuck in a non-Ready state).
    """
    client = InfrahubClientSync()
    definitions = client.all(kind="CoreArtifactDefinition")
    if not definitions:
        print("No CoreArtifactDefinitions registered; nothing to regenerate.")
        return 0

    expected = 0
    for definition in definitions:
        deleted = _delete_existing_artifacts(client, definition.id)
        _trigger_generate(client, definition.id)
        print(f"queued: {definition.name.value} (deleted {deleted} stale)")
        expected += deleted

    # Give the server a moment to start creating the new artifacts.
    time.sleep(POLL_INTERVAL_SECONDS)

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        artifacts = client.all(kind="CoreArtifact")
        if (
            len(artifacts) >= expected
            and artifacts
            and all(a.status.value == "Ready" for a in artifacts)
        ):
            print(f"All {len(artifacts)} artifacts Ready.")
            return 0
        time.sleep(POLL_INTERVAL_SECONDS)

    artifacts = client.all(kind="CoreArtifact")
    by_state = [(a.name.value, a.status.value) for a in artifacts]
    print(
        f"Timed out after {READY_TIMEOUT_SECONDS}s. "
        f"Expected ≥{expected} Ready, got {len(artifacts)}: {by_state}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
