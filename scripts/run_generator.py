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
from infrahub_sdk.exceptions import NodeNotFoundError

SYNC_TIMEOUT_SECONDS = 120
SYNC_POLL_INTERVAL_SECONDS = 3


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
    response = client.execute_graphql(
        """
        mutation Run($id: String!) {
          CoreGeneratorDefinitionRun(data: { id: $id }, wait_until_completion: true) {
            ok
          }
        }
        """,
        variables={"id": generator.id},
    )
    if not response.get("CoreGeneratorDefinitionRun", {}).get("ok"):
        print(f"Generator {args.name!r} run did not report ok: {response}", file=sys.stderr)
        return 1
    print(f"Generator {args.name!r} run completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
