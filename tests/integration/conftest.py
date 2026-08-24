"""Fixtures for the integration suite.

The suite runs the whole demo against a throwaway Infrahub stack booted by
``infrahub-testcontainers``, at the version declared in ``[dependency-groups] dev``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync
from infrahub_testcontainers import __version__ as testcontainers_version
from infrahub_testcontainers.container import PROJECT_ENV_VARIABLES
from infrahub_testcontainers.helpers import TestInfrahubDocker

from .stack_config import StackImage, resolve_stack_image

TEST_DIRECTORY = Path(__file__).parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent.parent

# The token the container seeds for its admin account. Read through os.environ first: an
# override never lands back in PROJECT_ENV_VARIABLES, so the dict alone can disagree with
# what the container is running.
_ADMIN_TOKEN_VARIABLE = "INFRAHUB_TESTING_INITIAL_ADMIN_TOKEN"
ADMIN_TOKEN = os.environ.get(_ADMIN_TOKEN_VARIABLE, PROJECT_ENV_VARIABLES[_ADMIN_TOKEN_VARIABLE])


@pytest.fixture(scope="session")
def stack_image() -> StackImage:
    """Image and tag the stack runs, resolved from the environment."""
    return resolve_stack_image(os.environ, testcontainers_version)


@pytest.fixture(scope="session")
def infrahub_version(stack_image: StackImage) -> str:
    """Infrahub image tag under test."""
    return stack_image.tag


@pytest.fixture(scope="session", autouse=True)
def _apply_stack_image_env(stack_image: StackImage) -> None:
    """Merge the resolved stack image into ``os.environ`` before the stack boots.

    Session-scoped and autouse for ordering: it must run before the class-scoped
    ``infrahub_compose``, which reads these variables to decide what to boot.
    """
    os.environ.update(stack_image.as_env())


# Tracked subtrees the Infrahub server never reads.
SNAPSHOT_EXCLUDE_PREFIXES = ("docs/", "tests/")


class TestInfrahubDockerWithClient(TestInfrahubDocker):
    """Base class adding SDK clients and a git-clonable project snapshot."""

    @pytest.fixture
    def async_client_main(self, infrahub_port: int) -> InfrahubClient:
        """Async client bound to the container, scoped per test.

        Function-scoped because a class-scoped client outlives its event loop.
        """
        return InfrahubClient(
            config=Config(address=f"http://localhost:{infrahub_port}", api_token=ADMIN_TOKEN)
        )

    @pytest.fixture(scope="class")
    def client_main(self, infrahub_port: int) -> InfrahubClientSync:
        """Sync client bound to the container, shared across the class."""
        return InfrahubClientSync(
            config=Config(address=f"http://localhost:{infrahub_port}", api_token=ADMIN_TOKEN)
        )

    @pytest.fixture(scope="class")
    def project_snapshot(self, tmp_directory: Path) -> Path:
        """Copy the git-tracked project into a temp dir for ``GitRepo``.

        ``GitRepo`` copies its ``src_directory`` wholesale, so the live working tree
        would drag in ``.venv`` and build output.

        Raises:
            RuntimeError: If the project directory is not a git working tree.
        """
        try:
            listing = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=PROJECT_DIRECTORY,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Cannot snapshot {PROJECT_DIRECTORY}: not a git working tree ({exc.stderr})"
            ) from exc

        snapshot = tmp_directory / "project-snapshot"
        snapshot.mkdir(parents=True, exist_ok=True)

        for relative in (entry for entry in listing.stdout.split("\0") if entry):
            if relative.startswith(SNAPSHOT_EXCLUDE_PREFIXES):
                continue
            source = PROJECT_DIRECTORY / relative
            # Submodules list as directories, staged-but-deleted files may be absent.
            if not source.is_file():
                continue
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        return snapshot

    @staticmethod
    def execute_command(
        command: str,
        address: str,
        concurrent_execution: int = 10,
        pagination_size: int = 50,
    ) -> subprocess.CompletedProcess[str]:
        """Run a shell command against the test container from the repo root.

        Overrides the base helper only to pin ``cwd``, so relative paths resolve as
        they do under ``invoke bootstrap``.
        """
        env = os.environ.copy()
        env["INFRAHUB_ADDRESS"] = address
        env["INFRAHUB_API_TOKEN"] = ADMIN_TOKEN
        env["INFRAHUB_MAX_CONCURRENT_EXECUTION"] = str(concurrent_execution)
        env["INFRAHUB_PAGINATION_SIZE"] = str(pagination_size)

        return subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=PROJECT_DIRECTORY,
        )


@pytest.fixture(scope="session")
def dataset_object_files() -> list[Path]:
    """Ordered bootstrap object files for the default dataset.

    Reuses ``tasks._dataset_files`` so load order cannot drift from ``invoke bootstrap``.
    """
    from tasks import INFRAHUB_DATASET, _dataset_files

    return _dataset_files(INFRAHUB_DATASET)
