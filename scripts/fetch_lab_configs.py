"""Download each labbed PE's startup config into ``lab/devices/<pe>.cfg``.

Used by ``invoke lab.deploy``. For every ``DcimDevice`` with ``role=pe``
whose platform has ``containerlab_os`` set, resolve the matching
``CoreArtifact`` (e.g. ``pe-arista-eos`` for cEOS, ``pe-nokia-srlinux``
for SR Linux) and write its rendered content to the output directory
so containerlab can mount it via the ``startup-config`` field in the
topology file.

Usage:
    uv run python scripts/fetch_lab_configs.py [--out-dir lab/devices]
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

from infrahub_sdk import InfrahubClientSync

# clab kind (containerlab_os) -> CoreArtifactDefinition name
KIND_TO_DEFINITION: dict[str, str] = {
    "ceos": "pe-arista-eos-config",
    "srl": "pe-nokia-srlinux-config",
}


def main() -> int:
    """Fetch one config artifact per labbed PE.

    Returns:
        Exit code (0 if at least one config was written, 1 otherwise).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default="lab/devices",
        help="Where to write the per-PE config files",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    client = InfrahubClientSync()
    pes = client.filters(kind="DcimDevice", role__value="pe", prefetch_relationships=True)

    written = 0
    for pe in pes:
        platform = pe.platform.peer if pe.platform and pe.platform.peer else None
        kind = platform.containerlab_os.value if platform else None
        if not kind or kind not in KIND_TO_DEFINITION:
            continue

        definition_name = KIND_TO_DEFINITION[kind]
        defn = client.get(kind="CoreArtifactDefinition", name__value=definition_name)
        artifacts = client.filters(
            kind="CoreArtifact",
            definition__ids=[defn.id],
            object__ids=[pe.id],
        )
        if not artifacts:
            print(
                f"warn: no {definition_name} artifact for {pe.name.value} (not generated yet?)",
                file=sys.stderr,
            )
            continue

        artifact = artifacts[0]
        storage_id = artifact.storage_id.value
        if not storage_id:
            print(
                f"warn: {definition_name} artifact for {pe.name.value} has no storage_id",
                file=sys.stderr,
            )
            continue

        url = f"{client.address}/api/storage/object/{storage_id}"
        req = urllib.request.Request(
            url,
            headers={"X-INFRAHUB-KEY": os.environ["INFRAHUB_API_TOKEN"]},
        )
        with urllib.request.urlopen(req) as resp:
            content = resp.read()

        out_path = out_dir / f"{pe.name.value}.cfg"
        out_path.write_bytes(content)
        print(f"wrote {out_path}")
        written += 1

    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
