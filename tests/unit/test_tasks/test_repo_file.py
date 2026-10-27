"""Repository object that ``invoke bootstrap`` registers.

The public sandbox registers a fork or another ref through ``INFRAHUB_REPO_URL`` and
``INFRAHUB_REPO_REF``, so the rendered object must carry both.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

import tasks


@pytest.fixture
def rendered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Render into a temporary file, with a ``git ls-remote`` probe that finds the ref."""
    target = tmp_path / "git-repo.yml"
    monkeypatch.setattr(tasks, "RENDERED_REPO_FILE", target)
    return target


def _context() -> MagicMock:
    context = MagicMock()
    context.run.return_value = MagicMock(ok=True, stdout="")
    return context


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
