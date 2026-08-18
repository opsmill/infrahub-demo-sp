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
from infrahub_testcontainers import __version__ as testcontainers_version
from infrahub_testcontainers.container import PROJECT_ENV_VARIABLES
from infrahub_testcontainers.helpers import TestInfrahubDocker

from .stack_config import StackImage, resolve_stack_image

TEST_DIRECTORY = Path(__file__).parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent.parent

# The container seeds this token for its initial admin account. Passing it
# explicitly keeps the suite self-contained: without it the clients fall back to
# whatever INFRAHUB_API_TOKEN happens to be in the environment, so mutations
# fail with AuthenticationError anywhere that variable isn't already set.
#
# Resolved through os.environ first, mirroring what the .env writer does at
# container.py:178-181 (`os.environ.get(key, value)`). Nothing merges an
# override back into PROJECT_ENV_VARIABLES, so reading the dict alone would hand
# the clients the packaged default while the container ran the override -- the
# same resolved-versus-running divergence `stack_config` exists to prevent,
# arriving through the token instead of the image.
_ADMIN_TOKEN_VARIABLE = "INFRAHUB_TESTING_INITIAL_ADMIN_TOKEN"
ADMIN_TOKEN = os.environ.get(_ADMIN_TOKEN_VARIABLE, PROJECT_ENV_VARIABLES[_ADMIN_TOKEN_VARIABLE])


@pytest.fixture(scope="session")
def stack_image() -> StackImage:
    """Image and tag the stack runs, resolved from the environment.

    This repository runs vanilla Infrahub -- no Dockerfile, no custom image --
    so it passes no repository, no default tag and no ``custom_build``, and the
    defaults are the vanilla case.

    Returns:
        The resolved image. See ``stack_config`` for why this does not read
        ``PROJECT_ENV_VARIABLES``.
    """
    return resolve_stack_image(os.environ, testcontainers_version)


@pytest.fixture(scope="session")
def infrahub_version(stack_image: StackImage) -> str:
    """Infrahub image tag under test.

    Args:
        stack_image: The resolved stack image.

    Returns:
        The resolved tag -- an explicit override when one is set, otherwise the
        installed ``infrahub-testcontainers`` version. The latter is the case a
        dependency-bump pull request exercises: moving the pin moves the version
        under test, with no CI wiring at all.
    """
    return stack_image.tag


@pytest.fixture(scope="session", autouse=True)
def _apply_stack_image_env(stack_image: StackImage) -> None:
    """Merge the resolved stack image into ``os.environ`` before the stack boots.

    Resolving ``stack_image`` above is not enough by itself: nothing else in this
    module writes it back into the environment, so an override such as
    ``INFRAHUB_TESTING_IMAGE_VERSION`` set alone would change what this fixture
    *reports* without changing what ``TestInfrahubDocker.infrahub_compose`` (in
    ``infrahub_testcontainers.helpers``) actually boots -- the exact divergence
    ``stack_config.as_env()`` exists to close. Note also that ``TestInfrahubDocker``
    defines its own class-scoped ``infrahub_version`` fixture
    (``helpers.py:22-24``), which pytest resolves in preference to the module-level
    one above for any test class deriving from it; merging into ``os.environ`` here
    reaches that fixture too, since it reads ``INFRAHUB_TESTING_IMAGE_VER`` directly.

    Ordering is why this is session-scoped and autouse rather than an explicit
    dependency of ``infrahub_compose``: pytest instantiates higher-scoped fixtures
    before narrower-scoped ones for the same test, so this session fixture runs
    before any class-scoped fixture -- including ``infrahub_compose`` and the
    ``infrahub_version``/``tmp_directory``/``remote_repos_dir`` fixtures it depends
    on -- for the first test in any class, in this or any later test module.

    Args:
        stack_image: The resolved image; ``as_env()`` sets both tag spellings plus
            the repository and pull flag so every consumer agrees.
    """
    os.environ.update(stack_image.as_env())


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
