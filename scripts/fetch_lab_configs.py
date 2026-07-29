"""Download each labbed device's startup config into ``lab/devices/<name>.cfg``.

Used by ``invoke lab.deploy``. For every ``DcimDevice`` that containerlab
boots — the backbone PEs (``role=pe``) and the pre-provisioned customer edge
routers (``role=cpe``) — whose platform has ``containerlab_os`` set, resolve
the matching ``CoreArtifact`` (e.g. ``pe-arista-eos`` for a cEOS PE,
``ce-arista-eos`` for a cEOS CE) and write its rendered content to the output
directory so containerlab can mount it via the ``startup-config`` field in the
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
from typing import Any

from infrahub_sdk import InfrahubClientSync

# (device role, clab kind) -> CoreArtifactDefinition name. A role/kind pair
# that isn't listed here has no lab-deployable config and is skipped.
DEFINITION_BY_ROLE_AND_KIND: dict[tuple[str, str], str] = {
    ("pe", "ceos"): "pe-arista-eos-config",
    ("pe", "srl"): "pe-nokia-srlinux-config",
    ("cpe", "ceos"): "ce-arista-eos-config",
}

# Device roles containerlab boots, in the order their configs are fetched.
LAB_ROLES = ("pe", "cpe")


def _artifact_content(
    client: InfrahubClientSync, definition_name: str, device: Any
) -> bytes | None:
    """Return the rendered artifact for a device, or ``None`` with a warning.

    Args:
        client: Active Infrahub SDK client.
        definition_name: Name of the CoreArtifactDefinition to resolve.
        device: The DcimDevice node the artifact targets.

    Returns:
        The artifact's rendered bytes, or ``None`` when it hasn't been
        generated yet or carries no storage object.
    """
    defn = client.get(kind="CoreArtifactDefinition", name__value=definition_name)
    artifacts = client.filters(
        kind="CoreArtifact",
        definition__ids=[defn.id],
        object__ids=[device.id],
    )
    if not artifacts:
        print(
            f"warn: no {definition_name} artifact for {device.name.value} (not generated yet?)",
            file=sys.stderr,
        )
        return None

    storage_id = artifacts[0].storage_id.value
    if not storage_id:
        print(
            f"warn: {definition_name} artifact for {device.name.value} has no storage_id",
            file=sys.stderr,
        )
        return None

    url = f"{client.address}/api/storage/object/{storage_id}"
    req = urllib.request.Request(
        url,
        headers={"X-INFRAHUB-KEY": os.environ["INFRAHUB_API_TOKEN"]},
    )
    with urllib.request.urlopen(req) as resp:
        content: bytes = resp.read()
    return content


def main() -> int:
    """Fetch one config artifact per labbed PE and CE.

    Returns:
        Exit code (0 if at least one config was written, 1 otherwise).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default="lab/devices",
        help="Where to write the per-device config files",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    client = InfrahubClientSync()

    written = 0
    for role in LAB_ROLES:
        devices = client.filters(kind="DcimDevice", role__value=role, prefetch_relationships=True)
        for device in devices:
            platform = device.platform.peer if device.platform and device.platform.peer else None
            kind = platform.containerlab_os.value if platform else None
            definition_name = DEFINITION_BY_ROLE_AND_KIND.get((role, kind)) if kind else None
            if not definition_name:
                continue

            content = _artifact_content(client, definition_name, device)
            if content is None:
                continue

            out_path = out_dir / f"{device.name.value}.cfg"
            out_path.write_bytes(content)
            print(f"wrote {out_path}")
            written += 1

    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
