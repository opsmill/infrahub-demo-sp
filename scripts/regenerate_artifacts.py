"""Force regeneration of every CoreArtifactDefinition and wait for Ready.

Bootstrap loads objects (which Infrahub auto-dispatches artifact builds
for) before the L3VPN generator runs. The first artifact attempt sees
empty VRF / PE-CE state and lands in ``Error``. Calling this script at
the end of bootstrap — after ``run_generator.py`` has materialized the
data — kicks each artifact definition and waits until every
``CoreArtifact`` row is ``Ready``.

``--branch`` does the same on one branch. ``invoke lab.deploy --branch`` uses
it to render a proposed change's artifacts before fetching them.

Usage:
    uv run python scripts/regenerate_artifacts.py [--branch <branch>]
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import quote

from infrahub_sdk import Config, InfrahubClientSync

READY_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 3


def _trigger_generate(client: InfrahubClientSync, definition_id: str, branch: str) -> None:
    """POST to Infrahub's artifact-generate endpoint for one definition on ``branch``.

    The endpoint renders on ``main`` unless ``?branch=`` names another branch,
    and the client's default branch does not reach this call. Without the
    parameter, a run against a branch deleted that branch's artifacts and then
    rendered main's instead. ``definition.generate()`` in the SDK posts without
    the parameter too, so the URL is built here and sent through the client,
    which adds its credentials and its timeout.

    Raises:
        httpx.HTTPStatusError: If Infrahub rejects the request.
    """
    url = f"{client.address}/api/artifact/generate/{definition_id}?branch={quote(branch, safe='')}"
    response = client._post(url=url, payload={"nodes": []})
    response.raise_for_status()


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


def main(argv: list[str] | None = None) -> int:
    """Delete + re-trigger every artifact, then wait for all to be Ready.

    Infrahub's artifact-generate endpoint dedups by effective inputs:
    if schema data hasn't changed, it no-ops even when the *template*
    has changed on disk. Delete-then-regenerate is the only reliable
    way to pick up template-only changes.

    Args:
        argv: Command-line arguments without the program name. ``None`` means
            no arguments, not ``sys.argv``, so a direct call never parses its
            caller's arguments.

    Returns:
        Exit code (0 if every artifact is Ready before the timeout,
        non-zero if any are stuck in a non-Ready state).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--branch",
        default=None,
        help="Infrahub branch to render on (default: the SDK's default branch, normally main)",
    )
    args = parser.parse_args(argv or [])
    client = (
        InfrahubClientSync(config=Config(default_branch=args.branch))
        if args.branch
        else InfrahubClientSync()
    )
    # Also covers INFRAHUB_DEFAULT_BRANCH, which sets the client's default
    # branch but would otherwise never reach the generate URL.
    branch = args.branch or client.default_branch
    definitions = client.all(kind="CoreArtifactDefinition")
    if not definitions:
        print("No CoreArtifactDefinitions registered; nothing to regenerate.")
        return 0

    expected = 0
    for definition in definitions:
        deleted = _delete_existing_artifacts(client, definition.id)
        _trigger_generate(client, definition.id, branch)
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
    raise SystemExit(main(sys.argv[1:]))
