"""Invoke tasks for the SP demo MPLS L3VPN repo."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from invoke.collection import Collection
from invoke.context import Context
from invoke.tasks import task

REPO_ROOT = Path(__file__).resolve().parent
COMPOSE_PROJECT = "sp-demo"
INFRAHUB_VERSION = os.getenv("INFRAHUB_VERSION", "stable")
INFRAHUB_SERVICE_CATALOG = os.getenv("INFRAHUB_SERVICE_CATALOG", "false").lower() == "true"
INFRAHUB_GIT_LOCAL = os.getenv("INFRAHUB_GIT_LOCAL", "false").lower() == "true"
LOCAL_COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
OVERRIDE_FILE = REPO_ROOT / "docker-compose.override.yml"


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
    cmd = f"curl -sf https://infrahub.opsmill.io/{INFRAHUB_VERSION} | {base} -f -"
    if OVERRIDE_FILE.exists():
        cmd += f" -f {OVERRIDE_FILE}"
    return cmd


def _compose(c: Context, args: str, profile: str | None = None) -> None:
    """Run docker compose with the demo project name and optional profile."""
    profile_arg = f"--profile {profile}" if profile else ""
    c.run(f"{_compose_base()} {profile_arg} {args}", pty=True)


@task
def start(c: Context, build: bool = False) -> None:
    """Start Infrahub containers.

    Set ``INFRAHUB_SERVICE_CATALOG=true`` in ``.env`` to also build and start the
    Streamlit service-catalog sidecar on every ``invoke start`` / ``invoke init``.
    """
    profile = "service-catalog" if INFRAHUB_SERVICE_CATALOG else None
    # Always pass --build when the catalog is enabled so local code changes
    # in service_catalog/ are picked up on every start.
    build_arg = "--build" if (build or INFRAHUB_SERVICE_CATALOG) else ""
    _compose(c, f"up -d {build_arg}", profile=profile)


@task
def destroy(c: Context) -> None:
    """Tear down Infrahub containers and volumes."""
    _compose(c, "down -v", profile="service-catalog")


@task
def bootstrap(c: Context) -> None:
    """Load schemas, menus, and bootstrap object data into Infrahub.

    A ``CoreRepository`` (local mount at ``/upstream``) or
    ``CoreReadOnlyRepository`` (public GitHub clone) is registered so the
    server can discover ``.infrahub.yml`` — transforms, artifact
    definitions, generators, and checks. Selection is driven by the
    ``INFRAHUB_GIT_LOCAL`` env var.
    """
    c.run("uv run infrahubctl schema load schemas/", pty=True)
    c.run("uv run infrahubctl menu load menus/menu.yml", pty=True)
    for path in sorted(Path("objects").glob("*.yml")):
        c.run(f"uv run infrahubctl object load {shlex.quote(str(path))}", pty=True)
    repo_file = (
        "objects/git-repo/local-dev.yml" if INFRAHUB_GIT_LOCAL else "objects/git-repo/github.yml"
    )
    c.run(f"uv run infrahubctl object load {shlex.quote(repo_file)}", pty=True)
    c.run(
        "uv run infrahubctl protocols --branch main --out generators/schema_protocols.py",
        pty=True,
    )


@task(name="init")
def init_demo(c: Context) -> None:
    """Destroy, start, and bootstrap the demo end-to-end."""
    destroy(c)
    start(c, build=True)
    c.run("sleep 30", pty=True)
    bootstrap(c)


@task
def lint(c: Context) -> None:
    """Run the full lint suite: ruff, mypy, yamllint."""
    c.run("uv run ruff check .", pty=True)
    c.run("uv run ruff format --check .", pty=True)
    c.run("uv run mypy .", pty=True)
    c.run("uv run yamllint .", pty=True)


@task
def test(c: Context, kind: str = "unit") -> None:
    """Run pytest; kind in {unit, integration, catalog, all}."""
    if kind == "all":
        c.run("uv run pytest tests/", pty=True)
    else:
        c.run(f"uv run pytest tests/{kind}/", pty=True)


@task
def docs(c: Context) -> None:
    """Build the Docusaurus documentation site under docs/."""
    with c.cd(str(REPO_ROOT / "docs")):
        c.run("pnpm install --frozen-lockfile", pty=True)
        c.run("pnpm run build", pty=True)


LAB_DIR = REPO_ROOT / "lab"
LAB_TOPO = LAB_DIR / "mpls-backbone.clab.yml"


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


# Lab namespace (filled in Phase 8)
lab = Collection("lab")


@task(name="deploy")
def lab_deploy(c: Context) -> None:
    """Fetch the clab topology artifact and run containerlab deploy."""
    LAB_DIR.mkdir(exist_ok=True)
    _fetch_artifact(c, "clab-mpls-topology", LAB_TOPO)
    c.run(f"containerlab deploy --topo {LAB_TOPO}", pty=True)


@task(name="destroy")
def lab_destroy(c: Context) -> None:
    """Tear down the running lab."""
    if not LAB_TOPO.exists():
        print(f"No lab topology at {LAB_TOPO}; nothing to destroy.")
        return
    c.run(f"containerlab destroy --topo {LAB_TOPO}", pty=True)


@task(name="status")
def lab_status(c: Context) -> None:
    """Show running clab containers."""
    if not LAB_TOPO.exists():
        print(f"No lab topology at {LAB_TOPO}.")
        return
    c.run(f"containerlab inspect --topo {LAB_TOPO}", pty=True)


@task(name="push-arista")
def lab_push_arista(c: Context) -> None:
    """Push the rendered Arista config to the running cEOS lab node."""
    LAB_DIR.mkdir(exist_ok=True)
    arista_cfg = LAB_DIR / "pe-lon-arista.cfg"
    _fetch_artifact(c, "pe-arista-eos", arista_cfg)
    c.run(
        f"uv run python scripts/push_arista.py {shlex.quote(str(arista_cfg))} pe-lon-arista",
        pty=True,
    )


lab.add_task(lab_deploy)
lab.add_task(lab_destroy)
lab.add_task(lab_status)
lab.add_task(lab_push_arista)

ns = Collection()
ns.add_task(start)
ns.add_task(destroy)
ns.add_task(bootstrap)
ns.add_task(init_demo)
ns.add_task(lint)
ns.add_task(test)
ns.add_task(docs)
ns.add_collection(lab)
