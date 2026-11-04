"""Repository object that ``invoke bootstrap`` registers.

The public sandbox registers a fork or another ref through ``INFRAHUB_REPO_URL`` and
``INFRAHUB_REPO_REF``, so the rendered object must carry both.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from invoke.config import Config
from invoke.context import Context
from invoke.exceptions import Exit

import tasks


@pytest.fixture
def rendered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Render into a temporary file instead of ``lab/git-repo.yml``."""
    target = tmp_path / "git-repo.yml"
    monkeypatch.setattr(tasks, "RENDERED_REPO_FILE", target)
    return target


def _context() -> MagicMock:
    context = MagicMock()
    context.run.return_value = MagicMock(ok=True, stdout="")
    return context


def _real_context() -> Context:
    """A context that runs the real ``git``; pytest blocks the stdin that invoke reads."""
    return Context(Config(overrides={"run": {"in_stream": False}}))


def _repository(path: Path) -> dict[str, str]:
    return yaml.safe_load(path.read_text())["spec"]["data"][0]


def test_the_default_location_is_the_opsmill_repository(
    rendered: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``INFRAHUB_REPO_URL`` the location from ``github.yml`` is kept."""
    monkeypatch.setattr(tasks, "INFRAHUB_GIT_LOCAL", False)
    monkeypatch.setattr(tasks, "INFRAHUB_REPO_URL", "")
    tasks._render_repo_file(_context(), "main")
    repository = _repository(rendered)
    assert repository["location"] == "https://github.com/opsmill/infrahub-demo-sp.git"
    assert repository["ref"] == "main"


def test_infrahub_repo_url_replaces_the_location(
    rendered: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork registers its own URL, and the probe checks the ref on that URL."""
    fork = "https://github.com/example/infrahub-demo-sp"
    monkeypatch.setattr(tasks, "INFRAHUB_GIT_LOCAL", False)
    monkeypatch.setattr(tasks, "INFRAHUB_REPO_URL", fork)
    context = _context()
    tasks._render_repo_file(context, "sandbox-setup")
    repository = _repository(rendered)
    assert repository["location"] == fork
    assert repository["ref"] == "sandbox-setup"
    assert fork in context.run.call_args.args[0]


def test_infrahub_repo_url_does_not_change_the_local_mount(
    rendered: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``INFRAHUB_GIT_LOCAL`` the server reads ``/upstream``, whatever the URL says."""
    monkeypatch.setattr(tasks, "INFRAHUB_GIT_LOCAL", True)
    monkeypatch.setattr(tasks, "INFRAHUB_REPO_URL", "https://github.com/example/infrahub-demo-sp")
    tasks._render_repo_file(_context(), "main")
    assert _repository(rendered)["location"] == "/upstream"


@pytest.fixture
def remote(tmp_path: Path) -> tuple[str, str]:
    """A real git repository with a branch, a lightweight tag and an annotated tag.

    Returns its location and the hash of its only commit.
    """
    source = tmp_path / "source"

    def git(*args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "tag.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    source.mkdir()
    git("init", "--quiet", "--initial-branch", "main")
    git("commit", "--quiet", "--allow-empty", "-m", "one")
    git("tag", "v1.2.0")
    git("tag", "--annotate", "-m", "release", "v1.3.0")
    git("branch", "feat/x")
    return source.as_uri(), git("rev-parse", "HEAD")


@pytest.mark.parametrize("ref", ["main", "feat/x", "v1.2.0", "v1.3.0"])
def test_a_branch_or_a_tag_on_the_remote_is_accepted(
    rendered: Path, monkeypatch: pytest.MonkeyPatch, remote: tuple[str, str], ref: str
) -> None:
    """The sandbox pipeline passes the ref of the demo: a branch or a tag."""
    location, _ = remote
    monkeypatch.setattr(tasks, "INFRAHUB_GIT_LOCAL", False)
    monkeypatch.setattr(tasks, "INFRAHUB_REPO_URL", location)
    tasks._render_repo_file(_real_context(), ref)
    assert _repository(rendered)["ref"] == ref


def test_a_commit_hash_is_accepted_without_a_probe(
    rendered: Path, monkeypatch: pytest.MonkeyPatch, remote: tuple[str, str]
) -> None:
    """``git ls-remote`` never lists a commit, so a full hash is not probed."""
    location, commit = remote
    monkeypatch.setattr(tasks, "INFRAHUB_GIT_LOCAL", False)
    monkeypatch.setattr(tasks, "INFRAHUB_REPO_URL", location)
    context = _context()
    tasks._render_repo_file(context, commit)
    assert _repository(rendered)["ref"] == commit
    context.run.assert_not_called()


@pytest.mark.parametrize("ref", ["missing", "v9.9.9", "0123456", "main-"])
def test_a_ref_that_is_not_on_the_remote_stops_bootstrap(
    rendered: Path, monkeypatch: pytest.MonkeyPatch, remote: tuple[str, str], ref: str
) -> None:
    """A missing ref, and a short hash that cannot be told apart from one, are refused."""
    location, _ = remote
    monkeypatch.setattr(tasks, "INFRAHUB_GIT_LOCAL", False)
    monkeypatch.setattr(tasks, "INFRAHUB_REPO_URL", location)
    with pytest.raises(Exit):
        tasks._render_repo_file(_real_context(), ref)
