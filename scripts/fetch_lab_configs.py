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

# Which kinds containerlab actually boots is decided in one place — the topology
# transform — so this script and the rendered topology cannot disagree about
# which devices need a startup-config.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from transforms.clab_topology import LABBED_KINDS  # noqa: E402

# (device role, clab kind) -> CoreArtifactDefinition name. Every lab-deployable
# role/kind pair needs an entry; a labbed device with no entry is an error, since
# the topology declares a startup-config for it regardless.
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
    missing: list[str] = []
    for role in LAB_ROLES:
        devices = client.filters(kind="DcimDevice", role__value=role, prefetch_relationships=True)
        for device in devices:
            platform = device.platform.peer if device.platform and device.platform.peer else None
            kind = platform.containerlab_os.value if platform else None
            if kind not in LABBED_KINDS:
                # Not lab-deployable at all: the topology transform skips it too,
                # so no startup-config is expected for it.
                continue
            definition_name = DEFINITION_BY_ROLE_AND_KIND.get((role, kind))
            if not definition_name:
                # Lab-deployable but unmapped: if the topology declares this
                # node it also declares a startup-config for it, so silence
                # here becomes a containerlab failure later. Say so and fail.
                print(
                    f"error: no artifact definition for role={role} kind={kind} "
                    f"({device.name.value}); add it to DEFINITION_BY_ROLE_AND_KIND",
                    file=sys.stderr,
                )
                missing.append(device.name.value)
                continue

            content = _artifact_content(client, definition_name, device)
            if content is None:
                missing.append(device.name.value)
                continue

            out_path = out_dir / f"{device.name.value}.cfg"
            out_path.write_bytes(content)
            print(f"wrote {out_path}")
            written += 1

    if missing:
        # Non-zero even when some configs were written: the rendered topology
        # names a startup-config for every node it declares, so a partial fetch
        # makes `containerlab deploy` abort on the missing file. Failing here
        # reports the real cause instead. A CE whose site the generator has not
        # materialised is not in the topology at all, so this can over-report —
        # the trade is a clear error over a cryptic containerlab one.
        print(
            f"error: no config written for {len(missing)} labbed device(s): "
            f"{', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        return 1
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
