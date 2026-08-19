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
from invoke.exceptions import Exit
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
# Git ref the server clones for transforms/generators/checks. Defaults to the
# branch you have checked out, NOT to `main` — see _github_repo_ref.
INFRAHUB_REPO_REF = os.getenv("INFRAHUB_REPO_REF", "")
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


def _error(msg: str) -> None:
    """Print a failure marker for a step that stops the run."""
    console.print(f"[red]✗[/red] {msg}")


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
GIT_REPO_DIR = REPO_ROOT / "objects" / "git-repo"
# Rendered repo object goes under lab/ because lab/* is gitignored — the ref
# is machine-specific and must never be committed.
RENDERED_REPO_FILE = REPO_ROOT / "lab" / "git-repo.yml"


def _repo_ref(c: Context) -> str:
    """Return the git ref the server should read `.infrahub.yml` code from.

    Object data is loaded from the working tree, but transforms, generators,
    queries and checks are read by the server from a *clone*. Pinning that
    clone to `main` while the working tree is on a feature branch runs new data
    against old code — and the failure is opaque ("One or more generators
    failed") because the traceback stays server-side. So the ref follows the
    checked-out branch by default.

    Args:
        c: Invoke context, used to shell out to git.

    Returns:
        The ref to use: ``INFRAHUB_REPO_REF`` if set, else the current branch,
        falling back to ``main`` when the branch can't be determined.
    """
    if INFRAHUB_REPO_REF:
        return INFRAHUB_REPO_REF
    result = c.run("git rev-parse --abbrev-ref HEAD", hide=True, warn=True)
    branch = (result.stdout or "").strip() if result.ok else ""
    if not branch or branch == "HEAD":  # detached
        return "main"
    return branch


def _render_repo_file(c: Context, ref: str) -> Path:
    """Write the repository object, pinned to ``ref``.

    Both registration modes pin a branch and both default to ``main`` on disk,
    so both need rewriting — a local mount pointed at ``main`` reads stale code
    just as surely as a GitHub clone does.

    Args:
        c: Invoke context, used to verify the ref is visible to the server.
        ref: Git ref to pin the repository to.

    Returns:
        Path to the rendered object file.

    Raises:
        Exit: If the ref is not present on the repository the server will clone.
            A local mount is only warned about, because a stale working tree
            still produces a usable (if outdated) clone; a missing remote ref
            produces nothing at all.
    """
    template = GIT_REPO_DIR / ("local-dev.yml" if INFRAHUB_GIT_LOCAL else "github.yml")
    spec = yaml.safe_load(template.read_text())
    # CoreRepository calls it `default_branch`; CoreReadOnlyRepository, `ref`.
    key = "default_branch" if INFRAHUB_GIT_LOCAL else "ref"
    spec["spec"]["data"][0][key] = ref
    RENDERED_REPO_FILE.parent.mkdir(parents=True, exist_ok=True)
    RENDERED_REPO_FILE.write_text(yaml.safe_dump(spec, sort_keys=False))

    if INFRAHUB_GIT_LOCAL:
        # The server clones the mounted repo, so it sees committed history
        # only — uncommitted edits to generators/transforms are invisible.
        dirty = c.run("git status --porcelain", hide=True, warn=True)
        if dirty.ok and (dirty.stdout or "").strip():
            _wait(
                "Working tree has uncommitted changes. The server clones committed "
                "history, so those edits will NOT be used — commit them first."
            )
    else:
        # The server clones over HTTPS, so an unpushed local branch is invisible
        # to it and the sync fails with a bare "couldn't find remote ref". Probe
        # the URL the server will actually clone, not the contributor's `origin`:
        # on a fork those differ, so probing `origin` reports a branch that is
        # present on the fork but absent from the repository being registered.
        location = spec["spec"]["data"][0]["location"]
        probe = c.run(
            f"git ls-remote --exit-code --heads {shlex.quote(location)} {shlex.quote(ref)}",
            hide=True,
            warn=True,
        )
        if not probe.ok:
            # Abort rather than warn. Continuing is guaranteed to fail, but not
            # for another two minutes and not with this message: the sync finds
            # no ref, so no CoreGeneratorDefinition is ever created and bootstrap
            # dies in scripts/run_generator.py on a timeout that says nothing
            # about the ref. A warning here was read as advisory and scrolled
            # past — by the time the run failed, the reason was 100 lines up.
            # Printed through the console, not passed to Exit: invoke writes an
            # Exit message out verbatim, so Rich markup would reach the terminal
            # as literal "[red]" text.
            _error(
                f"Ref '{ref}' is not on {location} — the server clones that URL over HTTPS "
                f"and will not find it, so no transforms, generators or checks would be "
                f"registered.\n"
                f"Push the branch there, set INFRAHUB_REPO_REF to a ref that exists, or set "
                f"INFRAHUB_GIT_LOCAL=true to mount the working tree instead."
            )
            raise Exit(code=1)
    return RENDERED_REPO_FILE


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

    ref = _repo_ref(c)
    repo_file = str(_render_repo_file(c, ref).relative_to(REPO_ROOT))
    source = "/upstream mount" if INFRAHUB_GIT_LOCAL else "github.com"
    kind = "CoreRepository" if INFRAHUB_GIT_LOCAL else "CoreReadOnlyRepository"
    _step(f"Registering {kind} ({source} @ {ref})")
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

    # Load the event triggers now that the CoreRepository has synced
    # .infrahub.yml (the run_generator steps above block until the
    # CoreGeneratorDefinition rows exist). The triggers reference those
    # definitions, so they can only load once the sync is complete — which
    # is why this file is not part of the bootstrap object set.
    _step("Loading event triggers (generator actions + group triggers)")
    c.run("uv run infrahubctl object load objects/events/00_triggers.yml", pty=True)
    _success("Event triggers loaded")

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


@task(name="lint-ruff")
def lint_ruff(c: Context) -> None:
    """Lint and check the formatting of all Python files with ruff.

    Args:
        c: Invoke Context.
    """
    _step("ruff check")
    # Two commands, not one: `ruff check` does not check formatting, and the
    # `format` task cannot stand in for it because `format` rewrites files and
    # would pass in CI regardless of what it rewrote. `ruff check --select I .`
    # is deliberately absent -- `I` is already in select, and --select sets
    # rather than extends the rule list, so it could never fail on its own.
    c.run("uv run ruff check .", pty=True)
    _step("ruff format --check")
    c.run("uv run ruff format --check --diff .", pty=True)


@task(name="lint-mypy")
def lint_mypy(c: Context) -> None:
    """Type-check all Python files with mypy.

    Args:
        c: Invoke Context.
    """
    _step("mypy")
    c.run("uv run mypy --show-error-codes .", pty=True)


@task(name="lint-yaml")
def lint_yaml(c: Context) -> None:
    """Lint all YAML files with yamllint.

    Args:
        c: Invoke Context.
    """
    _step("yamllint")
    # -s promotes warnings to errors. CI has always passed it and this task
    # never did; without it, routing CI through this task would silently end
    # warning-level YAML enforcement.
    c.run("uv run yamllint -s .", pty=True)


@task(name="lint-markdown")
def lint_markdown(c: Context) -> None:
    """Lint all Markdown files with rumdl.

    Args:
        c: Invoke Context.
    """
    _step("rumdl")
    # No --fail-on: rumdl's default `any` severity applies here and in CI alike,
    # because ci.yml's markdown-lint gate is this task. What the
    # repository tolerates is expressed as `disable` in pyproject.toml instead,
    # where both ends read it.
    c.run("uv run rumdl check .", pty=True)


@task
def lint(c: Context) -> None:
    """Run the full lint suite: markdown, YAML, ruff, mypy.

    Args:
        c: Invoke Context.
    """
    _banner("invoke lint", border="cyan")
    lint_markdown(c)
    lint_yaml(c)
    lint_ruff(c)
    lint_mypy(c)
    _success("Lint suite passed")


@task(name="format")
def format_code(c: Context) -> None:
    """Format all Python files with ruff, applying safe lint fixes.

    Args:
        c: Invoke Context.
    """
    _banner("invoke format", border="green")
    _step("ruff format")
    c.run("uv run ruff format .", pty=True)
    _step("ruff check --fix")
    c.run("uv run ruff check . --fix", pty=True)
    _success("Formatting completed")


@task(name="test-unit")
def test_unit(c: Context) -> None:
    """Run every test that needs no Infrahub deployment.

    Args:
        c: Invoke Context.
    """
    _banner("invoke test-unit", border="cyan")
    # Two invocations because the deployment-free tests live in two places: the
    # unit directory, and the integration tests marked `offline`, which read
    # only repository files and resolution logic. A single `-m offline` run over
    # tests/ would not cover tests/unit, and a marker selection matching nothing
    # exits 5 rather than 0, so the two are kept separate and explicit.
    c.run("uv run pytest tests/unit", pty=True)
    # `pytest.mark.offline` lives in exactly one place
    # (tests/integration/test_01_stack_config.py). If that marker is ever
    # dropped -- a cheap follow-up once the guard it exists
    # for is no longer needed -- this invocation would collect nothing and
    # exit 5, which would otherwise fail this task for a reason unrelated to
    # any test failing. Tolerate only that code here; any other non-zero exit
    # still fails the task.
    result = c.run("uv run pytest tests/integration -m offline", pty=True, warn=True)
    if result.exited not in (0, 5):
        raise Exit(
            f"uv run pytest tests/integration -m offline failed (exit {result.exited})",
            code=result.exited,
        )
    _success("Unit tests passed")


@task(
    name="test-integration",
    help={"tier": "core (default) runs everything but the extended tier; full runs all of it."},
)
def test_integration(c: Context, tier: str = "core") -> None:
    """Run the integration suite against a throwaway Infrahub deployment.

    Needs Docker. The Infrahub release under test is whatever
    `[dependency-groups] dev` pins ``infrahub-testcontainers`` to.

    Args:
        c: Invoke Context.
        tier: Either ``core`` or ``full``.
    """
    if tier not in {"core", "full"}:
        raise Exit(f"tier must be 'core' or 'full', got {tier!r}")
    _banner(f"invoke test-integration --tier {tier}", border="cyan")
    # `core` deselects the `extended` tests; `full` collects everything. The
    # proposed-change tail of tests/integration/test_workflow.py is what
    # currently carries `extended` -- see issue #111.
    marker = "" if tier == "full" else ' -m "not extended"'
    c.run(f"uv run pytest tests/integration{marker}", pty=True)
    _success(f"Integration tests passed ({tier})")


@task(name="test")
def test_all(c: Context) -> None:
    """Run every test in the repository, unit and integration.

    Args:
        c: Invoke Context.
    """
    _banner("invoke test", border="cyan")
    c.run("uv run pytest tests", pty=True)
    _success("Tests passed")


@task
def batfish(c: Context, backbone: str = "mpls-backbone") -> None:
    """Run BatfishBackboneCheck against a running local Infrahub.

    Requires:
        - `uv run invoke start` (Infrahub + Batfish sidecar + batfish-runner up)
        - INFRAHUB_ADDRESS, INFRAHUB_API_TOKEN set in .env
        - At least one rendered pe-* artifact in the local instance

    The check posts configs to the batfish-runner sidecar. From the host the
    runner is reached on its published port, so point BATFISH_RUNNER_URL at
    localhost (inside the task-worker it defaults to http://batfish-runner:8080).

    Args:
        c: Invoke Context.
        backbone: Backbone name to validate (default: mpls-backbone).
    """
    _banner("invoke batfish", border="cyan")
    runner_port = os.getenv("BATFISH_RUNNER_PORT", "8080")
    runner_url = os.getenv("BATFISH_RUNNER_URL", f"http://localhost:{runner_port}")
    cmd = (
        f"BATFISH_RUNNER_URL={runner_url} "
        f"uv run infrahubctl check batfish_backbone name={backbone} --branch main"
    )
    c.run(cmd, pty=True)


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
    """Push each rendered Arista config to its running cEOS lab node.

    Reuses the per-device configs fetched by ``invoke lab.deploy`` into
    ``lab/devices/<node>.cfg`` and pushes one per cEOS node in the topology,
    so it works for a single-Arista backbone (ISP) or an all-cEOS backbone
    (financial) alike.
    """
    _banner("invoke lab.push-arista", border="cyan")
    if not LAB_TOPO.exists():
        _wait(f"No lab topology at {LAB_TOPO}; run `invoke lab.deploy` first.")
        return
    # containerlab DNS-registers each node as clab-<lab-name>-<node-name>,
    # not clab-<node-name>. The lab name lives in the rendered topology
    # YAML, so parse it here rather than hard-code it.
    topo = yaml.safe_load(LAB_TOPO.read_text())
    lab_name = topo["name"]
    ceos_nodes = [
        node_name
        for node_name, node in topo.get("topology", {}).get("nodes", {}).items()
        if node.get("kind") == "ceos"
    ]
    if not ceos_nodes:
        _wait("No cEOS nodes in the topology; nothing to push.")
        return
    # Keep going past a node that fails. push_arista.py exits non-zero when a
    # node never becomes ready, and letting invoke raise on the first one left
    # every remaining node unconfigured — the opposite of what you want when
    # twelve cEOS containers are still settling. Failures are reported at the
    # end instead, the same way a missing config is.
    failed: list[str] = []
    pushed = 0
    for node_name in ceos_nodes:
        cfg = LAB_DEVICES_DIR / f"{node_name}.cfg"
        if not cfg.exists():
            _wait(f"No config at {cfg.relative_to(REPO_ROOT)}; run `invoke lab.deploy` first.")
            failed.append(node_name)
            continue
        host = f"clab-{lab_name}-{node_name}"
        _step(f"Pushing {cfg.relative_to(REPO_ROOT)} → {host}")
        result = c.run(
            f"uv run python scripts/push_arista.py {shlex.quote(str(cfg))} {shlex.quote(host)}",
            pty=True,
            warn=True,
        )
        if result.ok:
            pushed += 1
        else:
            failed.append(node_name)
    if failed:
        # Non-zero, not just a printed warning. Keeping going past a failed node
        # is right — the remaining eleven are still worth configuring — but
        # exiting 0 afterwards told `invoke lab.deploy && invoke lab.push-arista
        # && <checks>` that every router was configured when none might be.
        _error(
            f"Pushed {pushed}/{len(ceos_nodes)} node(s); failed: {', '.join(sorted(failed))}. "
            f"Re-run `invoke lab.push-arista` once they finish booting."
        )
        raise Exit(code=1)
    _success(f"Config pushed to {pushed} node(s)")


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
ns.add_task(lint_markdown)
ns.add_task(lint_yaml)
ns.add_task(lint_ruff)
ns.add_task(lint_mypy)
ns.add_task(lint)
ns.add_task(format_code)
ns.add_task(test_unit)
ns.add_task(test_integration)
ns.add_task(test_all)
ns.add_task(batfish)
ns.add_task(docs)
ns.add_collection(lab)
