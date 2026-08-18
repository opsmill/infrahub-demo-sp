"""Fixtures for the integration suite.

The suite runs the whole demo against a throwaway Infrahub stack booted by
``infrahub-testcontainers``. The pinned ``infrahub-testcontainers`` version in
``[dependency-groups] dev`` selects which Infrahub release is exercised, which
is what ``.github/workflows/update-infrahub.yml`` bumps when a new version
ships.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync
from infrahub_testcontainers.container import PROJECT_ENV_VARIABLES
from infrahub_testcontainers.helpers import TestInfrahubDocker

TEST_DIRECTORY = Path(__file__).parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent.parent

# The container seeds this token for its initial admin account. Passing it
# explicitly keeps the suite self-contained: without it the clients fall back to
# whatever INFRAHUB_API_TOKEN happens to be in the environment, so mutations
# fail with AuthenticationError anywhere that variable isn't already set.
ADMIN_TOKEN = PROJECT_ENV_VARIABLES["INFRAHUB_TESTING_INITIAL_ADMIN_TOKEN"]

# Tracked subtrees the Infrahub server never reads. Excluding them keeps the
# snapshot (and the clone the server makes from it) small.
SNAPSHOT_EXCLUDE_PREFIXES = ("docs/", "tests/")


class TestInfrahubDockerWithClient(TestInfrahubDocker):
    """Base class adding SDK clients and a git-clonable project snapshot."""

    @pytest.fixture
    def async_client_main(self, infrahub_port: int) -> InfrahubClient:
        """Async client bound to the container, scoped per test.

        Function scope is deliberate: pytest-asyncio gives each async test its
        own event loop, and a class-scoped client would carry HTTP transport
        bound to an already-closed loop into later tests.

        Returns:
            InfrahubClient pointed at the test container.
        """
        return InfrahubClient(
            config=Config(address=f"http://localhost:{infrahub_port}", api_token=ADMIN_TOKEN)
        )

    @pytest.fixture(scope="class")
    def client_main(self, infrahub_port: int) -> InfrahubClientSync:
        """Sync client bound to the container, shared across the class.

        Returns:
            InfrahubClientSync pointed at the test container.
        """
        return InfrahubClientSync(
            config=Config(address=f"http://localhost:{infrahub_port}", api_token=ADMIN_TOKEN)
        )

    @pytest.fixture(scope="class")
    def project_snapshot(self, tmp_directory: Path) -> Path:
        """Copy the git-tracked project into a temp dir for ``GitRepo``.

        ``GitRepo`` copies its ``src_directory`` wholesale, so pointing it at
        the live working tree would drag in ``.venv`` and any build output.
        Copying only tracked files gives the server the same content a real
        clone would see.

        Returns:
            Path to the snapshot directory.

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
            # Submodule entries appear as directories and staged-but-deleted
            # files may be absent; skip anything that isn't a readable file.
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

        Overrides the base helper only to pin ``cwd`` to the project root, so
        relative paths in ``infrahubctl`` invocations resolve the same way they
        do in ``invoke bootstrap``.

        Args:
            command: Shell command to run.
            address: Infrahub server address.
            concurrent_execution: Value for ``INFRAHUB_MAX_CONCURRENT_EXECUTION``.
            pagination_size: Value for ``INFRAHUB_PAGINATION_SIZE``.

        Returns:
            The completed process, with stdout and stderr captured.
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

    Reuses ``tasks._dataset_files`` so the suite loads objects in exactly the
    order ``invoke bootstrap`` does; a reordering in ``tasks.py`` cannot drift
    away from what the test exercises.

    Returns:
        Ordered list of YAML object files.
    """
    from tasks import INFRAHUB_DATASET, _dataset_files

    return _dataset_files(INFRAHUB_DATASET)
