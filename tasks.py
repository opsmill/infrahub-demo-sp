"""Invoke tasks for the infrahub-demo-sp repo."""

from __future__ import annotations

import importlib.metadata
import os
import shlex
import time
from pathlib import Path

import yaml
from invoke.collection import Collection
from invoke.context import Context
from invoke.tasks import task
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console()

REPO_ROOT = Path(__file__).resolve().parent
COMPOSE_PROJECT = "sp-demo"
INFRAHUB_VERSION = os.getenv("INFRAHUB_VERSION", "stable")
INFRAHUB_SERVICE_CATALOG = os.getenv("INFRAHUB_SERVICE_CATALOG", "false").lower() == "true"
INFRAHUB_GIT_LOCAL = os.getenv("INFRAHUB_GIT_LOCAL", "false").lower() == "true"
INFRAHUB_DATASET = os.getenv("INFRAHUB_DATASET", "financial")
INFRAHUB_ENTERPRISE = os.getenv("INFRAHUB_ENTERPRISE", "false").lower() == "true"
LOCAL_COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
OVERRIDE_FILE = REPO_ROOT / "docker-compose.override.yml"


def _banner(title: str, body: str = "", border: str = "cyan") -> None:
    """Print a Rich panel with a colored border and optional body.

    Args:
        title: Heading shown in the panel border.
        body: Optional multi-line body rendered inside the panel.
        border: Rich color name for the border + title style.
    """
    content = body or f"[bold {border}]{title}[/bold {border}]"
    title_arg = f"[bold]{title}[/bold]" if body else None
    console.print()
    console.print(Panel(content, title=title_arg, border_style=border, box=box.SIMPLE))


def _step(msg: str) -> None:
    """Print an in-progress step marker."""
    console.print(f"[cyan]→[/cyan] {msg}")


def _wait(msg: str) -> None:
    """Print a waiting / pending step marker."""
    console.print(f"[yellow]→[/yellow] {msg}")


def _success(msg: str) -> None:
    """Print a success marker."""
    console.print(f"[green]✓[/green] {msg}")


def _sleep_with_progress(seconds: int, description: str) -> None:
    """Sleep for ``seconds``, drawing a Rich progress bar each second.

    Args:
        seconds: How long to sleep.
        description: Label shown alongside the progress bar.
    """
    with Progress(
        SpinnerColumn(spinner_name="dots12", style="bold bright_yellow"),
        TextColumn("[progress.description]{task.description}", style="bold white"),
        BarColumn(bar_width=40, style="yellow", complete_style="bright_green"),
        TextColumn("[bold bright_cyan]{task.percentage:>3.0f}%"),
        TextColumn("•", style="dim"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        bar = progress.add_task(description, total=seconds)
        for _ in range(seconds):
            time.sleep(1)
            progress.update(bar, advance=1)


def _compose_base() -> str:
    """Build the docker compose invocation, sourcing the base file locally or upstream.

    Mirrors infrahub-demo-dc: if a local ``docker-compose.yml`` exists, use it; otherwise
    stream the file from ``https://infrahub.opsmill.io/<version>`` via ``docker compose -f -``.
    The committed ``docker-compose.override.yml`` is always layered on top.
    """
    base = f"docker compose -p {COMPOSE_PROJECT}"
    if LOCAL_COMPOSE_FILE.exists():
        cmd = f"{base} -f {LOCAL_COMPOSE_FILE}"
        if OVERRIDE_FILE.exists():
            cmd += f" -f {OVERRIDE_FILE}"
        return cmd
    edition_path = f"enterprise/{INFRAHUB_VERSION}" if INFRAHUB_ENTERPRISE else INFRAHUB_VERSION
    cmd = f"curl -sf https://infrahub.opsmill.io/{edition_path} | {base} -f -"
    if OVERRIDE_FILE.exists():
        cmd += f" -f {OVERRIDE_FILE}"
    return cmd


def _compose(c: Context, args: str, profile: str | None = None) -> None:
    """Run docker compose with the demo project name and optional profile."""
    profile_arg = f"--profile {profile}" if profile else ""
    c.run(f"{_compose_base()} {profile_arg} {args}", pty=True)


def _compose_source() -> str:
    """Human-readable description of where the base compose file comes from."""
    if LOCAL_COMPOSE_FILE.exists():
        return "Local (docker-compose.yml)"
    edition = "Enterprise" if INFRAHUB_ENTERPRISE else "Community"
    return f"infrahub.opsmill.io ({edition} {INFRAHUB_VERSION})"


def _task_summary(t: object) -> str:
    """Return the first line of the task's docstring, or a placeholder."""
    body = (t.__doc__ or "").strip()
    return body.split("\n", 1)[0] if body else "(no description)"


@task(name="list")
def list_tasks(c: Context) -> None:
    """List every available invoke task with its description."""
    rows: list[tuple[str, str]] = [(t.name, _task_summary(t)) for t in ns.tasks.values()]
    rows.extend((f"lab.{t.name}", _task_summary(t)) for t in lab.tasks.values())
    rows.sort(key=lambda row: row[0])
    table = Table(
        title="Available invoke tasks",
        box=box.SIMPLE,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Task", style="green", no_wrap=True)
    table.add_column("Description", style="white")
    for name, description in rows:
        table.add_row(name, description)
    console.print()
    console.print(table)
    console.print()


def _running_infrahub_version() -> str:
    """Query the running Infrahub server for its actual version.

    Returns "(server not reachable)" if the API can't be hit — this task
    needs to work even when containers are down.
    """
    try:
        import json
        import urllib.request

        address = os.getenv("INFRAHUB_ADDRESS", "http://localhost:8000")
        token = os.getenv("INFRAHUB_API_TOKEN", "")
        req = urllib.request.Request(
            f"{address}/api/info",
            headers={"X-INFRAHUB-KEY": token} if token else {},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read()).get("version", "unknown")
    except Exception:
        return "(server not reachable)"


@task
def info(c: Context) -> None:
    """Show the current demo configuration."""
    try:
        sdk_version = importlib.metadata.version("infrahub-sdk")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = "unknown"
    edition = "Enterprise" if INFRAHUB_ENTERPRISE else "Community"
    body = (
        f"[cyan]Project:[/cyan]          {COMPOSE_PROJECT}\n"
        f"[cyan]Infrahub running:[/cyan] {_running_infrahub_version()}\n"
        f"[cyan]Infrahub SDK:[/cyan]     {sdk_version}\n"
        f"[cyan]Edition:[/cyan]          {edition} [dim](INFRAHUB_ENTERPRISE env var)[/dim]\n"
        f"[cyan]Compose tag:[/cyan]      {INFRAHUB_VERSION} [dim](INFRAHUB_VERSION env var)[/dim]\n"
        f"[cyan]Compose source:[/cyan]   {_compose_source()}\n"
        f"[cyan]Dataset:[/cyan]          {INFRAHUB_DATASET}\n"
        f"[cyan]Local git:[/cyan]        {'enabled' if INFRAHUB_GIT_LOCAL else 'disabled'}\n"
        f"[cyan]Service catalog:[/cyan]  {'enabled' if INFRAHUB_SERVICE_CATALOG else 'disabled'}"
    )
    _banner("Infrahub demo-sp configuration", body=body, border="blue")


@task
def start(c: Context, build: bool = False) -> None:
    """Start Infrahub containers.

    Set ``INFRAHUB_SERVICE_CATALOG=true`` in ``.env`` to also build and start the
    Streamlit service-catalog sidecar on every ``invoke start`` / ``invoke init``.
    """
    catalog_on = INFRAHUB_SERVICE_CATALOG
    rebuild = build or catalog_on
    body = (
        f"[green]Starting Infrahub[/green] [dim]({INFRAHUB_VERSION})[/dim]\n"
        f"[dim]Project:[/dim]         {COMPOSE_PROJECT}\n"
        f"[dim]Compose source:[/dim] {_compose_source()}\n"
        f"[dim]Service catalog:[/dim] {'enabled' if catalog_on else 'disabled'}\n"
        f"[dim]Local git:[/dim]      {'enabled' if INFRAHUB_GIT_LOCAL else 'disabled'}"
        + ("\n[yellow]Rebuild:[/yellow] enabled" if rebuild else "")
    )
    _banner("invoke start", body=body, border="green")
    profile = "service-catalog" if catalog_on else None
    build_arg = "--build" if rebuild else ""
    _compose(c, f"up -d {build_arg}", profile=profile)
    _success("Infrahub UI:      http://localhost:8000  (admin / infrahub)")
    if catalog_on:
        _success("Service catalog:  http://localhost:8501")


@task
def destroy(c: Context) -> None:
    """Tear down Infrahub containers and volumes."""
    _banner("invoke destroy", border="red")
    _wait("Removing containers and volumes")
    _compose(c, "down -v", profile="service-catalog")
    _success("Infrahub torn down")


DATASETS_DIR = REPO_ROOT / "objects" / "datasets"


def _dataset_files(dataset: str) -> list[Path]:
    """Return the ordered list of YAML files to load for ``dataset``.

    Merges shared ``objects/*.yml`` with the dataset-specific overlay at
    ``objects/datasets/<dataset>/*.yml``. Sorting is by basename so the
    numeric prefixes (``00_*``, ``02_*``, ``20_*``, ``80_*``) interleave
    in the right load order regardless of which directory each file
    lives in.

    Raises:
        ValueError: If ``dataset`` is not a directory under ``DATASETS_DIR``.
    """
    overlay_dir = DATASETS_DIR / dataset
    if not overlay_dir.is_dir():
        available = ", ".join(sorted(p.name for p in DATASETS_DIR.iterdir() if p.is_dir()))
        raise ValueError(f"Unknown dataset {dataset!r}. Available: {available}")
    shared = [p for p in (REPO_ROOT / "objects").glob("*.yml")]
    overlay = list(overlay_dir.glob("*.yml"))
    return sorted(shared + overlay, key=lambda p: p.name)


@task
def bootstrap(c: Context) -> None:
    """Load schemas, menus, and bootstrap object data into Infrahub.

    A ``CoreRepository`` (local mount at ``/upstream``) or
    ``CoreReadOnlyRepository`` (public GitHub clone) is registered so the
    server can discover ``.infrahub.yml`` — transforms, artifact
    definitions, generators, and checks. Selection is driven by the
    ``INFRAHUB_GIT_LOCAL`` env var.

    The customer-facing overlay is selected by the ``INFRAHUB_DATASET``
    env var (default: ``financial``). Choices live under
    ``objects/datasets/``; ship with ``financial`` and ``isp``.
    """
    paths = _dataset_files(INFRAHUB_DATASET)
    _banner(f"invoke bootstrap (dataset: {INFRAHUB_DATASET})", border="cyan")

    _step("Loading schemas")
    c.run("uv run infrahubctl schema load schemas/", pty=True)
    _success("Schemas loaded")

    _step("Loading sidebar menu")
    c.run("uv run infrahubctl menu load menus/menu.yml", pty=True)
    _success("Menu loaded")

    _step(f"Loading bootstrap objects ({len(paths)} files)")
    for path in paths:
        c.run(f"uv run infrahubctl object load {shlex.quote(str(path))}", pty=True)
    _success("Bootstrap objects loaded")

    repo_file = (
        "objects/git-repo/local-dev.yml" if INFRAHUB_GIT_LOCAL else "objects/git-repo/github.yml"
    )
    _step(f"Registering CoreRepository ({repo_file})")
    c.run(f"uv run infrahubctl object load {shlex.quote(repo_file)}", pty=True)
    _success("CoreRepository registered")

    _step("Exporting Python protocols from the live schema")
    c.run(
        "uv run infrahubctl protocols --branch main --out generators/schema_protocols.py",
        pty=True,
    )
    _success("Protocols exported")

    # Force the L3VPN generator to run synchronously. Without this the
    # automatic generator dispatch races with artifact generation —
    # artifacts kick off before the VRF/IPs are materialized and end up
    # in `Error` state, requiring a manual re-trigger.
    _step("Running the L3VPN generator")
    c.run("uv run python scripts/run_generator.py generate_l3vpn", pty=True)
    _success("L3VPN generator complete")

    _step("Running the SD-WAN generator")
    c.run("uv run python scripts/run_generator.py generate_sdwan", pty=True)
    _success("SD-WAN generator complete")

    # Now that the generator has materialized the data the templates
    # depend on, regenerate every artifact — Infrahub's earlier
    # auto-dispatch ran against incomplete state and left artifacts in
    # `Error`. This converges every CoreArtifact to `Ready`.
    _step("Regenerating artifacts")
    c.run("uv run python scripts/regenerate_artifacts.py", pty=True)
    _success("All artifacts ready")

    console.print()
    _banner("Bootstrap complete", border="green")


@task(name="init")
def init_demo(c: Context) -> None:
    """Destroy, start, and bootstrap the demo end-to-end.

    The customer-facing dataset is selected by the ``INFRAHUB_DATASET``
    env var (default ``financial``); see ``.env.example``.
    """
    _banner(
        "invoke init",
        body=(
            "[bold]Full reset of the infrahub-demo-sp stack[/bold]\n"
            f"[dim]Dataset:[/dim] {INFRAHUB_DATASET}"
        ),
        border="magenta",
    )
    destroy(c)
    start(c, build=True)
    _wait("Waiting 30s for containers to settle")
    _sleep_with_progress(30, "containers warming up")
    bootstrap(c)
    console.print()
    _banner(
        "infrahub-demo-sp ready",
        body=(
            "[green]✓[/green] Infrahub UI:      http://localhost:8000  (admin / infrahub)\n"
            + (
                "[green]✓[/green] Service catalog:  http://localhost:8501\n"
                if INFRAHUB_SERVICE_CATALOG
                else ""
            )
            + "[dim]Try:[/dim] uv run invoke info"
        ),
        border="green",
    )


@task
def lint(c: Context) -> None:
    """Run the full lint suite: ruff, mypy, yamllint."""
    _banner("invoke lint", border="cyan")
    _step("ruff check")
    c.run("uv run ruff check .", pty=True)
    _step("ruff format --check")
    c.run("uv run ruff format --check .", pty=True)
    _step("mypy")
    c.run("uv run mypy .", pty=True)
    _step("yamllint")
    c.run("uv run yamllint .", pty=True)
    _success("Lint suite passed")


@task
def test(c: Context, kind: str = "unit") -> None:
    """Run pytest; kind in {unit, integration, catalog, all}."""
    _banner(f"invoke test --kind {kind}", border="cyan")
    target = "tests/" if kind == "all" else f"tests/{kind}/"
    c.run(f"uv run pytest {target}", pty=True)
    _success(f"{kind} tests passed")


@task
def docs(c: Context) -> None:
    """Build the Docusaurus documentation site under docs/."""
    _banner("invoke docs", border="cyan")
    with c.cd(str(REPO_ROOT / "docs")):
        _step("pnpm install")
        c.run("pnpm install --frozen-lockfile", pty=True)
        _step("pnpm run build")
        c.run("pnpm run build", pty=True)
    _success("Docusaurus site built")


LAB_DIR = REPO_ROOT / "lab"
LAB_TOPO = LAB_DIR / "mpls-backbone.clab.yml"
LAB_DEVICES_DIR = LAB_DIR / "devices"


def _fetch_artifact(c: Context, artifact_name: str, dest: Path) -> None:
    """Download the latest artifact content into ``dest``.

    Infrahub serves rendered artifacts from ``/api/storage/object/<id>``.
    ``scripts/fetch_artifact.py`` resolves the artifact name to a
    ``storage_id`` and streams the bytes to stdout, which we redirect.
    """
    c.run(
        f"uv run python scripts/fetch_artifact.py {shlex.quote(artifact_name)} "
        f"> {shlex.quote(str(dest))}",
        pty=False,
    )


# Lab namespace
lab = Collection("lab")


@task(name="deploy")
def lab_deploy(c: Context) -> None:
    """Fetch the clab topology artifact + per-PE configs, then deploy."""
    _banner("invoke lab.deploy", border="cyan")
    LAB_DIR.mkdir(exist_ok=True)
    LAB_DEVICES_DIR.mkdir(exist_ok=True)
    # Re-render artifacts against the latest committed template state.
    # Without this, a template change on main needs a manual
    # `regenerate_artifacts.py` run before the artifact you fetch picks
    # up the new content — easy to forget.
    _step("Regenerating artifacts (template fixes are picked up here)")
    c.run("uv run python scripts/regenerate_artifacts.py", pty=True)
    _success("Artifacts re-rendered")
    _step(f"Fetching clab-mpls-topology → {LAB_TOPO.relative_to(REPO_ROOT)}")
    _fetch_artifact(c, "clab-mpls-topology", LAB_TOPO)
    _success("Topology artifact fetched")
    _step(f"Fetching per-PE startup configs → {LAB_DEVICES_DIR.relative_to(REPO_ROOT)}/")
    c.run(
        f"uv run python scripts/fetch_lab_configs.py --out-dir {shlex.quote(str(LAB_DEVICES_DIR))}",
        pty=True,
    )
    _success("Per-PE configs fetched")
    # clab 0.71.1's deploy/destroy state model is leaky: even after a
    # successful `destroy --cleanup` there can be leftover Docker objects
    # (the management bridge network, containers from a previous partial
    # deploy) that make the next deploy fail with
    #   "The 'mpls-backbone-1' lab has already been deployed."
    # Belt-and-braces cleanup: clab destroy, then nuke any matching docker
    # containers + the named management network.
    lab_name = yaml.safe_load(LAB_TOPO.read_text())["name"]
    _step("Tearing down any prior lab state")
    c.run(
        f"containerlab destroy --cleanup --topo {LAB_TOPO}",
        pty=True,
        warn=True,
    )
    # Force-remove any docker containers and the management network whose
    # names start with `clab-<lab>-`. Run via `sh -c` so the shell expands
    # the command substitution; `|| true` keeps the task going when nothing
    # matches.
    container_filter = f"name=clab-{lab_name}-"
    c.run(
        f"sh -c '"
        f'orphans=$(docker ps -aq --filter "{container_filter}"); '
        f'[ -n "$orphans" ] && docker rm -f $orphans || true'
        f"'",
        pty=False,
        warn=True,
    )
    c.run(f"docker network rm clab-{shlex.quote(lab_name)} 2>/dev/null || true", warn=True)
    _step("Running containerlab deploy")
    c.run(f"containerlab deploy --topo {LAB_TOPO}", pty=True)
    _success("Lab deployed")


@task(name="destroy")
def lab_destroy(c: Context) -> None:
    """Tear down the running lab."""
    _banner("invoke lab.destroy", border="red")
    if not LAB_TOPO.exists():
        _wait(f"No lab topology at {LAB_TOPO}; nothing to destroy.")
        return
    c.run(f"containerlab destroy --topo {LAB_TOPO}", pty=True)
    _success("Lab destroyed")


@task(name="status")
def lab_status(c: Context) -> None:
    """Show running clab containers."""
    _banner("invoke lab.status", border="cyan")
    if not LAB_TOPO.exists():
        _wait(f"No lab topology at {LAB_TOPO}.")
        return
    c.run(f"containerlab inspect --topo {LAB_TOPO}", pty=True)


@task(name="push-arista")
def lab_push_arista(c: Context) -> None:
    """Push the rendered Arista config to the running cEOS lab node."""
    _banner("invoke lab.push-arista", border="cyan")
    LAB_DIR.mkdir(exist_ok=True)
    arista_cfg = LAB_DIR / "pe-lon-arista.cfg"
    _step(f"Fetching pe-arista-eos → {arista_cfg.relative_to(REPO_ROOT)}")
    _fetch_artifact(c, "pe-arista-eos", arista_cfg)
    _success("Artifact fetched")
    # containerlab DNS-registers each node as clab-<lab-name>-<node-name>,
    # not clab-<node-name>. The lab name lives in the rendered topology
    # YAML, so parse it here rather than hard-code it.
    lab_name = yaml.safe_load(LAB_TOPO.read_text())["name"]
    host = f"clab-{lab_name}-pe-lon-arista"
    _step(f"Pushing config to {host}")
    c.run(
        f"uv run python scripts/push_arista.py {shlex.quote(str(arista_cfg))} {shlex.quote(host)}",
        pty=True,
    )
    _success("Config pushed")


lab.add_task(lab_deploy)
lab.add_task(lab_destroy)
lab.add_task(lab_status)
lab.add_task(lab_push_arista)

ns = Collection()
ns.add_task(list_tasks)
ns.add_task(info)
ns.add_task(start)
ns.add_task(destroy)
ns.add_task(bootstrap)
ns.add_task(init_demo)
ns.add_task(lint)
ns.add_task(test)
ns.add_task(docs)
ns.add_collection(lab)
