"""Add a PE router on a new branch and open a proposed change, from the command line.

``uv run invoke demo-add-device`` runs this. It does what the service catalog's
Add PE page (``service_catalog/pages/4_Add_PE.py``) does, in the same order, so
a demo gives the same result from either one:

1. Validate the request with the page's own form rules.
2. Create a branch, ``backbone/add-pe-<name>-<suffix>`` unless one is named.
3. Create a ``TopologyPeOnboarding`` request on the branch and add it to the
   ``pe_onboardings`` group. Joining the group fires ``trigger-pe-generator``,
   which runs ``generate_pe`` on the branch: the device, its loopback, the core
   links to each uplink, IS-IS, LDP and the iBGP full mesh.
4. Wait until the generator has written every core port and iBGP session,
   reading its output rather than sleeping a fixed time.
5. Render every artifact on the branch again, so the proposed change shows the
   new PE's configuration, each neighbor's new session and the lab topology.
6. Open a proposed change into ``main`` and print its link.

Every option has a default that works on both datasets: the next free
``pe-NN`` name, the first two PEs on the backbone as uplinks, the Arista EOS
platform (the one the lab can boot), and the first uplink's site and device type.

Usage:
    uv run python scripts/demo_add_pe.py [--name pe-09] [--uplinks pe-02,pe-05]
        [--platform arista_eos] [--site fra] [--device-type 7280R3] [--branch <name>]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infrahub_sdk import Config, InfrahubClientSync
from rich import box
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

# The page's validators and progress query are plain Python, importable without
# Streamlit, so the command line and the page apply one set of rules.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts.regenerate_artifacts import _trigger_generate  # noqa: E402
from service_catalog.utils.validators import (  # noqa: E402
    PE_ONBOARDING_PROGRESS_QUERY,
    pe_is_materialised,
    validate_add_pe_form,
)

DEFAULT_PLATFORM = "arista_eos"
# The page's uplink picker allows at most four.
MAX_PE_UPLINKS = 4
# Consecutive complete readings that end the wait, as on the page: one reading
# can land while the generator is still writing.
REQUIRED_STABLE_PASSES = 2
POLL_INTERVAL_SECONDS = 3
# The page waits 180s by default. A demo host is often busier, and a timeout
# here costs a re-run, so allow longer.
DEFAULT_WAIT_SECONDS = int(os.environ.get("GENERATOR_WAIT_TIME", "300"))
PE_NAME_NUMBER = re.compile(r"^pe-(\d+)$")

console = Console()


@dataclass(frozen=True)
class PeRequest:
    """Everything the onboarding request needs, after defaults are applied."""

    name: str
    backbone: str
    backbone_pes: list[str]
    uplinks: list[str]
    platform: str
    site: str
    device_type: str


def next_pe_name(existing_device_names: set[str]) -> str:
    """Return ``pe-NN`` one past the highest numbered PE name in use.

    Args:
        existing_device_names: Names of every device on main.

    Returns:
        ``pe-09`` when ``pe-01`` to ``pe-08`` exist, ``pe-01`` when no device
        follows the ``pe-NN`` pattern.
    """
    numbers = [
        int(match.group(1))
        for name in existing_device_names
        if (match := PE_NAME_NUMBER.match(name))
    ]
    return f"pe-{max(numbers, default=0) + 1:02d}"


def resolve_request(
    *,
    pes: list[Any],
    backbones: dict[str, list[str]],
    existing_device_names: set[str],
    name: str | None,
    uplinks: list[str] | None,
    platform: str,
    site: str | None,
    device_type: str | None,
) -> tuple[PeRequest | None, list[str]]:
    """Apply defaults to the command-line options and validate the result.

    Args:
        pes: PE devices on main, with ``location`` and ``device_type`` fetched.
        backbones: PE names per backbone name.
        existing_device_names: Names of every device on main.
        name: Requested PE name, or ``None`` for the next free ``pe-NN``.
        uplinks: Requested uplink PE names, or ``None`` for the first two PEs
            of the backbone.
        platform: Platform name for the new PE.
        site: Site shortname, or ``None`` for the first uplink's site.
        device_type: Device type name, or ``None`` for the first uplink's.

    Returns:
        The resolved request and an empty list, or ``None`` and the errors
        that stop it.
    """
    if not backbones:
        return None, ["No TopologyMplsBackbone exists on main. Run `invoke bootstrap` first."]
    if uplinks:
        backbone = next(
            (bb for bb, members in sorted(backbones.items()) if uplinks[0] in members),
            sorted(backbones)[0],
        )
    else:
        backbone = sorted(backbones)[0]
        uplinks = sorted(backbones[backbone])[:2]
    name = name or next_pe_name(existing_device_names)

    errors = validate_add_pe_form(
        name=name,
        platform=platform,
        existing_device_names=existing_device_names,
        uplinks=uplinks,
        backbone_pes=set(backbones[backbone]),
    )
    if len(set(uplinks)) > MAX_PE_UPLINKS:
        errors.append(f"Pick at most {MAX_PE_UPLINKS} uplink PEs.")
    if errors:
        return None, errors

    first_uplink = next(pe for pe in pes if pe.name.value == uplinks[0])
    return (
        PeRequest(
            name=name,
            backbone=backbone,
            backbone_pes=sorted(backbones[backbone]),
            uplinks=uplinks,
            platform=platform,
            site=site or first_uplink.location.peer.shortname.value,
            device_type=device_type or first_uplink.device_type.peer.name.value,
        ),
        [],
    )


def _read_main(client: InfrahubClientSync) -> tuple[list[Any], dict[str, list[str]], set[str]]:
    """Read the PEs, the backbones and the device names the request is built from.

    Args:
        client: Client bound to main.

    Returns:
        The PE devices, PE names per backbone name, and every device name.
    """
    pes = client.filters(kind="DcimDevice", role__value="pe", prefetch_relationships=True)
    pe_name_by_id = {pe.id: pe.name.value for pe in pes}
    backbones: dict[str, list[str]] = {}
    for backbone in client.all(kind="TopologyMplsBackbone"):
        backbone.pes.fetch()
        backbones[backbone.name.value] = sorted(
            pe_name_by_id[peer.id] for peer in backbone.pes.peers if peer.id in pe_name_by_id
        )
    names = {device.name.value for device in client.all(kind="DcimDevice")}
    return pes, backbones, names


def _generator_finished(client: InfrahubClientSync, request: PeRequest, branch: str) -> bool:
    """Return whether the PE generator has written everything for ``request``.

    Args:
        client: Client bound to the branch.
        request: The onboarding request being waited on.
        branch: Branch the request was created on.

    Returns:
        True when the device exists, every uplink has a core port and every
        other PE has an iBGP session with it.
    """
    result = client.execute_graphql(
        query=PE_ONBOARDING_PROGRESS_QUERY,
        variables={"name": request.name},
        branch_name=branch,
    )
    edges = (result.get("TopologyPeOnboarding") or {}).get("edges") or []
    return pe_is_materialised(
        edges[0]["node"] if edges else None,
        core_interface_count=int((result.get("InterfacePhysical") or {}).get("count", 0)),
        ibgp_session_count=int((result.get("RoutingBGPSession") or {}).get("count", 0)),
        expected_uplinks=len(set(request.uplinks)),
        expected_sessions=len(request.backbone_pes),
    )


def _wait_for_generator(
    client: InfrahubClientSync, request: PeRequest, branch: str, wait_seconds: int
) -> bool:
    """Poll the generator's output until it is complete twice in a row.

    Args:
        client: Client bound to the branch.
        request: The onboarding request being waited on.
        branch: Branch the request was created on.
        wait_seconds: How long to wait before giving up.

    Returns:
        True if the generator finished within ``wait_seconds``.
    """
    deadline = time.monotonic() + wait_seconds
    stable = 0
    with Progress(
        SpinnerColumn(spinner_name="dots12", style="bold bright_yellow"),
        TextColumn("[progress.description]{task.description}", style="bold white"),
        BarColumn(bar_width=40, style="yellow", complete_style="bright_green"),
        TextColumn("•", style="dim"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        bar = progress.add_task("⏳ PE generator building the device", total=None)
        while time.monotonic() < deadline:
            stable = stable + 1 if _generator_finished(client, request, branch) else 0
            if stable >= REQUIRED_STABLE_PASSES:
                progress.update(bar, total=1, completed=1)
                return True
            time.sleep(POLL_INTERVAL_SECONDS)
    return False


def run(request: PeRequest, branch: str, wait_seconds: int, ui_url: str) -> int:
    """Create the branch, the request and the proposed change for ``request``.

    Args:
        request: The resolved, validated onboarding request.
        branch: Branch to create.
        wait_seconds: How long to wait for the PE generator.
        ui_url: Infrahub web address for the printed links.

    Returns:
        0 when the proposed change is open, 1 when the generator timed out.
    """
    main_client = InfrahubClientSync()
    console.print(f"\n[cyan]→[/cyan] Creating branch [bold]{branch}[/bold]")
    main_client.branch.create(branch, sync_with_git=False)
    client = InfrahubClientSync(config=Config(default_branch=branch))

    console.print(f"[cyan]→[/cyan] Creating the onboarding request for [bold]{request.name}[/bold]")
    onboarding = client.create(
        kind="TopologyPeOnboarding",
        name=request.name,
        description=f"Added by `invoke demo-add-device`, uplinks {', '.join(request.uplinks)}",
        backbone={"hfid": [request.backbone]},
        location={"hfid": [request.site]},
        platform={"hfid": [request.platform]},
        device_type={"hfid": [request.device_type]},
        uplinks=[{"hfid": [uplink]} for uplink in request.uplinks],
    )
    onboarding.save()
    # Joining the group fires `trigger-pe-generator` (objects/events/00_triggers.yml).
    # A relationship add rather than members.add() + save(): a save replaces the
    # whole member list.
    group = client.get(kind="CoreStandardGroup", name__value="pe_onboardings")
    group.add_relationships(relation_to_update="members", related_nodes=[onboarding.id])

    if not _wait_for_generator(client, request, branch, wait_seconds):
        console.print(
            f"[red]✗[/red] The PE generator did not finish within {wait_seconds}s, so no "
            f"proposed change was opened. Branch [bold]{branch}[/bold] is kept for inspection.\n"
            "  Check that `trigger-pe-generator` exists (loaded by `invoke bootstrap`) and "
            "read the task-worker logs:\n"
            "  docker compose -p sp-demo logs task-worker --tail 200"
        )
        return 1
    console.print("[green]✓[/green] Device, core links, loopback and iBGP sessions created")

    console.print("[cyan]→[/cyan] Rendering every artifact on the branch")
    for definition in client.all(kind="CoreArtifactDefinition"):
        _trigger_generate(client, definition.id, branch)

    console.print("[cyan]→[/cyan] Opening the proposed change into main")
    proposed_change = main_client.create(
        kind="CoreProposedChange",
        source_branch=branch,
        destination_branch="main",
        name=f"Add PE {request.name}",
    )
    proposed_change.save()

    pc_url = f"{ui_url}/proposed-changes/{proposed_change.id}"
    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column(style="cyan")
    table.add_column()
    table.add_row("Proposed change", f"Add PE {request.name}")
    table.add_row("Branch", branch)
    table.add_row("Uplinks", ", ".join(request.uplinks))
    table.add_row("Platform / site", f"{request.platform} / {request.site}")
    console.print(table)
    console.print("[bold green]✓ Proposed change opened[/bold green]")
    # Not in a panel: a panel wraps a long URL at the terminal width, which
    # breaks it in two for anyone copying or clicking it.
    console.print("[bold]Review the proposed change:[/bold]")
    console.print(f"  [link={pc_url}]{pc_url}[/link]", soft_wrap=True)
    console.print("[bold]Boot it in the lab before merging:[/bold]")
    console.print(f"  uv run invoke lab.deploy --branch {branch}", soft_wrap=True, markup=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse options, resolve defaults against main, then run the workflow.

    Args:
        argv: Command-line arguments without the program name. ``None`` means
            no arguments, not ``sys.argv``.

    Returns:
        Exit code: 0 on success, 1 on invalid options or a generator timeout.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--name", help="New PE name (default: next free pe-NN)")
    parser.add_argument(
        "--uplinks",
        help="Comma-separated existing PEs to link to, 2 to 4 (default: first two PEs)",
    )
    parser.add_argument("--platform", default=DEFAULT_PLATFORM, help="Platform of the new PE")
    parser.add_argument("--site", help="Site shortname (default: the first uplink's site)")
    parser.add_argument("--device-type", help="Device type (default: the first uplink's)")
    parser.add_argument("--branch", help="Branch to create (default: backbone/add-pe-<name>-<id>)")
    parser.add_argument(
        "--wait",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help="Seconds to wait for the PE generator",
    )
    args = parser.parse_args(argv or [])
    uplinks = [u.strip() for u in args.uplinks.split(",") if u.strip()] if args.uplinks else None

    pes, backbones, names = _read_main(InfrahubClientSync())
    request, errors = resolve_request(
        pes=pes,
        backbones=backbones,
        existing_device_names=names,
        name=args.name,
        uplinks=uplinks,
        platform=args.platform,
        site=args.site,
        device_type=args.device_type,
    )
    if request is None:
        for error in errors:
            console.print(f"[red]✗[/red] {error}")
        return 1

    branch = args.branch or f"backbone/add-pe-{request.name}-{uuid.uuid4().hex[:8]}"
    ui_url = os.environ.get("INFRAHUB_UI_URL", "http://localhost:8000").rstrip("/")
    return run(request, branch, args.wait, ui_url)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
