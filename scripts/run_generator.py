"""Trigger a server-side ``CoreGeneratorDefinition`` run, waiting for completion.

Bootstrap creates ``ServiceL3Vpn`` rows but Infrahub's automatic
generator dispatch can race with artifact generation — the artifact
flow fires before the VRF/IPs are materialized, leaving artifacts in
``Error`` status. Calling this script explicitly at the end of bootstrap
forces the generator to run and waits for it to finish before subsequent
steps (e.g. artifact generation) proceed.

Usage:
    uv run python scripts/run_generator.py generate_l3vpn
"""

from __future__ import annotations

import argparse
import sys
import time

from infrahub_sdk import InfrahubClientSync
from infrahub_sdk.exceptions import GraphQLError, NodeNotFoundError

SYNC_TIMEOUT_SECONDS = 120
SYNC_POLL_INTERVAL_SECONDS = 3
# The run is started without waiting, then its task is polled: one request held open for the
# whole run hit the SDK's 60-second client timeout, and Cloudflare, in front of the public
# sandboxes, ends a request after 100 seconds.
RUN_TIMEOUT_SECONDS = 600
RUN_POLL_INTERVAL_SECONDS = 3
FINAL_TASK_STATES = {"COMPLETED", "FAILED", "CANCELLED", "CRASHED"}

_FAILURE_HINT = """
The generator's traceback is server-side. To see it:

    docker compose -p sp-demo logs task-worker --tail 200

Most common cause: the server runs the code it CLONED, not your working tree.
If the clone is pinned to a different ref than the branch you loaded data
from, new data meets old code. Check the registered ref:

    cat lab/git-repo.yml        # the ref bootstrap actually registered

and either push your branch (bootstrap pins the clone to the checked-out
branch), or set INFRAHUB_GIT_LOCAL=true to mount the working tree instead.
""".strip()


def _wait_for_generator(client: InfrahubClientSync, name: str) -> object:
    """Return the named CoreGeneratorDefinition, waiting for repo sync.

    Bootstrap registers the repository immediately before calling this
    script, but the server-side git sync (which discovers
    ``.infrahub.yml`` and instantiates ``CoreGeneratorDefinition`` rows)
    runs asynchronously and can take 30-60s on a cold clone. Poll instead
    of failing immediately.

    Raises:
        TimeoutError: If the generator does not appear within the timeout.
    """
    deadline = time.monotonic() + SYNC_TIMEOUT_SECONDS
    while True:
        try:
            return client.get(kind="CoreGeneratorDefinition", name__value=name)
        except NodeNotFoundError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Generator {name!r} did not appear within {SYNC_TIMEOUT_SECONDS}s; "
                    "is the CoreRepository sync stuck?"
                ) from None
            time.sleep(SYNC_POLL_INTERVAL_SECONDS)


def _wait_for_task(client: InfrahubClientSync, task_id: str) -> str:
    """Poll the generator run's task until it ends, and return its final state.

    Returns:
        The task state: ``COMPLETED`` on success, another final state on
        failure, or the last state seen when the run outlasts the timeout.
    """
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    state = "PENDING"
    while True:
        response = client.execute_graphql(
            "query Task($ids: [String]) { InfrahubTask(ids: $ids) { edges { node { state } } } }",
            variables={"ids": [task_id]},
        )
        edges = response["InfrahubTask"]["edges"]
        if edges:
            state = edges[0]["node"]["state"]
        if state in FINAL_TASK_STATES or time.monotonic() >= deadline:
            return state
        time.sleep(RUN_POLL_INTERVAL_SECONDS)


def main() -> int:
    """Trigger a generator by name and wait for completion.

    Returns:
        Exit code (0 on success, non-zero if the generator can't be found
        or the mutation reports a failure).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="CoreGeneratorDefinition name (e.g. generate_l3vpn)")
    args = parser.parse_args()

    client = InfrahubClientSync()
    generator = _wait_for_generator(client, args.name)
    try:
        response = client.execute_graphql(
            """
            mutation Run($id: String!) {
              CoreGeneratorDefinitionRun(data: { id: $id }, wait_until_completion: false) {
                ok
                task { id }
              }
            }
            """,
            variables={"id": generator.id},
        )
    except GraphQLError as exc:
        print(f"Generator {args.name!r} could not start: {exc}", file=sys.stderr)
        return 1
    run = response.get("CoreGeneratorDefinitionRun") or {}
    if not run.get("ok") or not run.get("task"):
        print(f"Generator {args.name!r} run did not report ok: {response}", file=sys.stderr)
        return 1
    state = _wait_for_task(client, run["task"]["id"])
    if state != "COMPLETED":
        # The task holds only a state: the real traceback stays in the task
        # worker. Say where to find it, and name the usual cause.
        print(f"Generator {args.name!r} run ended {state}.", file=sys.stderr)
        print(_FAILURE_HINT, file=sys.stderr)
        return 1
    print(f"Generator {args.name!r} run completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
