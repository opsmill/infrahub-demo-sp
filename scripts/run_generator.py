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

import httpx
from infrahub_sdk import InfrahubClientSync
from infrahub_sdk.exceptions import (
    GraphQLError,
    JsonDecodeError,
    NodeNotFoundError,
    RateLimitError,
    ServerNotReachableError,
    ServerNotResponsiveError,
)

SYNC_TIMEOUT_SECONDS = 120
SYNC_POLL_INTERVAL_SECONDS = 3
# The run is started without waiting, then its task is polled: one request held open for the
# whole run hit the SDK's 60-second client timeout, and Cloudflare, in front of the public
# sandboxes, ends a request after 100 seconds.
RUN_TIMEOUT_SECONDS = 600
RUN_POLL_INTERVAL_SECONDS = 3
FINAL_TASK_STATES = {"COMPLETED", "FAILED", "CANCELLED", "CRASHED"}
# What the SDK raises when one status request fails and the run may still be fine on the
# server: a refused or dropped connection, a read timeout, a 429 that outlasts the SDK's own
# retries, a 502 or 524 page from Cloudflare (an HTML body, which the SDK reports as a JSON
# decoding error), a 5xx JSON body without a ``data`` key (a KeyError inside the SDK) and an
# error answer of Infrahub itself. A refused token is not in the list: waiting does not fix it.
_TRANSIENT_ERRORS = (
    httpx.HTTPError,
    GraphQLError,
    JsonDecodeError,
    KeyError,
    RateLimitError,
    ServerNotReachableError,
    ServerNotResponsiveError,
)


class TaskStateUnreadableError(RuntimeError):
    """The state of the run could not be read before the deadline."""


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

    A status request that fails is asked again until the deadline: the run goes on
    in the task worker whatever happens to one request.

    Returns:
        The task state: ``COMPLETED`` on success, another final state on
        failure, or the last state seen when the run outlasts the timeout.

    Raises:
        TaskStateUnreadableError: If the deadline passes while the last status request
            was failing, so the state of the run is not known.
    """
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    state = "PENDING"
    last_error = ""
    while True:
        error = ""
        try:
            response = client.execute_graphql(
                "query Task($ids: [String]) "
                "{ InfrahubTask(ids: $ids) { edges { node { state } } } }",
                variables={"ids": [task_id]},
            )
        except _TRANSIENT_ERRORS as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error != last_error:  # a persistent error is printed once, not every 3 seconds
                print(
                    f"Could not read the state of task {task_id}, asking again. {error}",
                    file=sys.stderr,
                )
            last_error = error
        else:
            last_error = ""
            edges = response["InfrahubTask"]["edges"]
            if edges:
                state = edges[0]["node"]["state"]
        if state in FINAL_TASK_STATES:
            return state
        if time.monotonic() >= deadline:
            if error:
                raise TaskStateUnreadableError(
                    f"The state of task {task_id} could not be read in the last "
                    f"{RUN_TIMEOUT_SECONDS} s. Last error: {error}"
                )
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
    try:
        state = _wait_for_task(client, run["task"]["id"])
    except TaskStateUnreadableError as exc:
        # The run may have ended: the traceback hint below does not apply.
        print(f"Generator {args.name!r}: {exc}", file=sys.stderr)
        return 1
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
