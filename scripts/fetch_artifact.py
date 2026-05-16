"""Stream the content of an Infrahub artifact to stdout.

``infrahubctl`` has no ``artifact get`` subcommand. This helper resolves
a ``CoreArtifact`` by name, then fetches its rendered content from the
Infrahub storage endpoint and writes the bytes to stdout so callers can
redirect to a file.

Usage:
    uv run python scripts/fetch_artifact.py pe-arista-eos > pe.cfg
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request

from infrahub_sdk.client_sync import InfrahubClientSync


def main() -> int:
    """Resolve artifact, fetch its content, write bytes to stdout.

    Returns:
        Exit code (0 on success, non-zero on missing artifact / content).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="CoreArtifact name (e.g. pe-arista-eos)")
    args = parser.parse_args()

    client = InfrahubClientSync()
    artifact = client.get(kind="CoreArtifact", name__value=args.name)
    storage_id = artifact.storage_id.value
    if not storage_id:
        print(
            f"Artifact {args.name!r} has no storage_id (not generated yet?)",
            file=sys.stderr,
        )
        return 1

    url = f"{client.address}/api/storage/object/{storage_id}"
    req = urllib.request.Request(
        url,
        headers={"X-INFRAHUB-KEY": os.environ["INFRAHUB_API_TOKEN"]},
    )
    with urllib.request.urlopen(req) as resp:
        sys.stdout.buffer.write(resp.read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
