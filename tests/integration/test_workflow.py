# pyright: reportAttributeAccessIssue=false
"""End-to-end demo workflow against a containerised Infrahub.

Walks the same path a user follows in the README, in two halves:

Bootstrap (mirrors ``invoke bootstrap``)
    schemas -> menu -> bootstrap objects -> protocol export -> repository
    registration -> L3VPN generator -> SD-WAN generator -> event triggers ->
    artifact regeneration.

Service request (mirrors what the Streamlit catalog does)
    branch -> load a ServiceL3Vpn -> group trigger fires the generator ->
    diff -> proposed change -> validations -> merge -> verify on main.

This is the suite the Infrahub version-bump automation runs: when
``.github/workflows/update-infrahub.yml`` bumps ``infrahub-testcontainers``
and opens a PR, CI runs this against the new release. Anything the upgrade
breaks — schema syntax, SDK API, generator or transform behaviour, the
proposed-change pipeline — surfaces here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.task.models import TaskState
from infrahub_sdk.testing.repository import GitRepo

from .conftest import PROJECT_DIRECTORY, TestInfrahubDockerWithClient

REPO_NAME = "infrahub-demo-sp"
SERVICE_BRANCH = "integration-add-l3vpn"
BOOTSTRAP_VPN = "trading-floor-vpn"
BRANCH_VPN = "integration-test-vpn"
BRANCH_REQUEST_FILE = "tests/integration/data/l3vpn_branch_request.yml"

# Polling budgets. Generous because a cold container pulls images, syncs the
# repository, and renders every artifact before the suite gets going.
REPO_SYNC_ATTEMPTS = 60
REPO_SYNC_INTERVAL = 10
GENERATOR_DEFINITION_ATTEMPTS = 18
GENERATOR_DEFINITION_INTERVAL = 10
GENERATOR_TASK_TIMEOUT = 1800
TRIGGERED_GENERATOR_ATTEMPTS = 40
TRIGGERED_GENERATOR_INTERVAL = 15
ARTIFACT_ATTEMPTS = 40
ARTIFACT_INTERVAL = 15
DIFF_TASK_TIMEOUT = 600
MERGE_TASK_TIMEOUT = 600
VALIDATION_ATTEMPTS = 40
VALIDATION_INTERVAL = 30
MERGE_PROPAGATION_DELAY = 10

# The batfish_backbone check posts device configs to the batfish-runner sidecar
# from docker-compose.override.yml. The testcontainers stack has no such
# sidecar, and its task-worker environment is a fixed allowlist, so the check's
# BATFISH_DISABLED escape hatch cannot be set from here either. The check would
# therefore always conclude failure — and Infrahub refuses to merge a proposed
# change with a failing check, which would make the merge path untestable. The
# suite drops this one definition before branching so every remaining validator
# is required to pass.
SIDECAR_DEPENDENT_CHECK = "batfish_backbone"

T = TypeVar("T")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestSpDemoWorkflow(TestInfrahubDockerWithClient):
    """The full service-provider demo workflow, start to finish."""

    @pytest.fixture(scope="class")
    def default_branch(self) -> str:
        """Branch the service request is staged on.

        Returns:
            Branch name used for the proposed-change half of the suite.
        """
        return SERVICE_BRANCH

    @pytest.fixture(scope="class")
    def workflow_state(self) -> dict[str, Any]:
        """IDs and task handles shared between ordered tests.

        Returns:
            Mutable dict carried across the class.
        """
        return {}

    @staticmethod
    async def wait_for(
        check: Callable[[], Awaitable[tuple[bool, T]]],
        attempts: int,
        interval: int,
        description: str,
    ) -> T:
        """Poll an async predicate until it reports done.

        Args:
            check: Coroutine returning ``(done, value)``.
            attempts: Maximum number of polls.
            interval: Seconds between polls.
            description: Label used in log lines and the timeout message.

        Returns:
            The value from the first poll that reported done.

        Raises:
            TimeoutError: If ``check`` never reports done.
        """
        for attempt in range(1, attempts + 1):
            done, value = await check()
            if done:
                return value
            logging.info("waiting for %s (%d/%d)", description, attempt, attempts)
            await asyncio.sleep(interval)

        raise TimeoutError(f"timed out waiting for {description} after {attempts * interval}s")

    @staticmethod
    def task_log_tail(task: Any, entries: int = 20) -> str:
        """Format the tail of a task's log for an assertion message.

        Args:
            task: Task returned by ``wait_for_completion``.
            entries: How many trailing log entries to include.

        Returns:
            Newline-joined log tail, or a placeholder when the task carries none.
        """
        logs = getattr(task, "logs", None) or []
        if not logs:
            return "(no task logs returned)"
        return "\n".join(f"    [{log.severity}] {log.message}" for log in logs[-entries:])

    @staticmethod
    def assert_command_ok(result: Any, what: str) -> None:
        """Assert a captured subprocess succeeded, quoting both streams.

        Args:
            result: CompletedProcess from ``execute_command``.
            what: Human-readable description of the step.
        """
        logging.info("%s stdout:\n%s", what, result.stdout)
        if result.stderr:
            logging.info("%s stderr:\n%s", what, result.stderr)
        assert result.returncode == 0, (
            f"{what} failed.\n"
            f"  return code: {result.returncode}\n"
            f"  stdout: {result.stdout}\n"
            f"  stderr: {result.stderr}"
        )

    # ---------------------------------------------------------------- bootstrap

    @pytest.mark.order(1)
    @pytest.mark.dependency(name="schemas")
    def test_01_load_schemas(self, client_main: InfrahubClientSync) -> None:
        """Load every schema under ``schemas/``."""
        result = self.execute_command(
            "infrahubctl schema load schemas/ --wait 120",
            address=client_main.config.address,
        )
        self.assert_command_ok(result, "schema load")

    @pytest.mark.order(2)
    @pytest.mark.dependency(name="menu", depends=["schemas"])
    def test_02_load_menu(self, client_main: InfrahubClientSync) -> None:
        """Load the sidebar menu definition."""
        result = self.execute_command(
            "infrahubctl menu load menus/menu.yml",
            address=client_main.config.address,
        )
        self.assert_command_ok(result, "menu load")

    @pytest.mark.order(3)
    @pytest.mark.dependency(name="objects", depends=["schemas"])
    def test_03_load_bootstrap_objects(
        self,
        client_main: InfrahubClientSync,
        dataset_object_files: list[Path],
    ) -> None:
        """Load the dataset object files in ``invoke bootstrap`` order."""
        assert dataset_object_files, "no dataset object files resolved from tasks._dataset_files"

        for path in dataset_object_files:
            relative = path.relative_to(PROJECT_DIRECTORY)
            result = self.execute_command(
                f"infrahubctl object load {relative}",
                address=client_main.config.address,
            )
            self.assert_command_ok(result, f"object load {relative}")

    @pytest.mark.order(4)
    @pytest.mark.dependency(name="protocols", depends=["objects"])
    def test_04_export_protocols(self, client_main: InfrahubClientSync, tmp_path: Path) -> None:
        """Export Python protocols from the live schema.

        Writes to a temp path rather than ``generators/schema_protocols.py``:
        the point is to prove the exporter still works against this Infrahub
        release, not to rewrite a tracked file mid-test.
        """
        out = tmp_path / "schema_protocols.py"
        result = self.execute_command(
            f"infrahubctl protocols --branch main --out {out}",
            address=client_main.config.address,
        )
        self.assert_command_ok(result, "protocols export")
        assert out.exists() and out.stat().st_size > 0, f"protocol export wrote nothing to {out}"

    @pytest.mark.order(5)
    @pytest.mark.dependency(name="repository", depends=["objects"])
    async def test_05_add_repository(
        self,
        async_client_main: InfrahubClient,
        project_snapshot: Path,
        remote_repos_dir: Path,
        workflow_state: dict[str, Any],
    ) -> None:
        """Register the project as a ``CoreRepository`` and wait for sync.

        The sync is what discovers ``.infrahub.yml`` and instantiates the
        generator, transform, artifact, and check definitions the rest of the
        suite depends on.
        """
        repo = GitRepo(
            name=REPO_NAME,
            src_directory=project_snapshot,
            dst_directory=remote_repos_dir,
        )

        response = await repo.add_to_infrahub(client=async_client_main)
        assert response.get(f"{repo.type.value}Create", {}).get("ok"), (
            f"failed to register repository {REPO_NAME}.\n  response: {response}"
        )

        async def synced() -> tuple[bool, Any]:
            repository = await async_client_main.get(kind=repo.type.value, name__value=REPO_NAME)
            status = repository.sync_status.value
            if "error" in status:
                raise AssertionError(
                    f"repository sync failed with status {status!r}; "
                    "check the server log for an import traceback"
                )
            return status == "in-sync", repository

        try:
            repository = await self.wait_for(
                synced,
                attempts=REPO_SYNC_ATTEMPTS,
                interval=REPO_SYNC_INTERVAL,
                description=f"repository {REPO_NAME} to reach in-sync",
            )
        except TimeoutError as exc:
            current = await async_client_main.get(kind=repo.type.value, name__value=REPO_NAME)
            raise AssertionError(
                f"repository {REPO_NAME} never synced.\n"
                f"  final status: {current.sync_status.value}\n"
                f"  waited: {REPO_SYNC_ATTEMPTS * REPO_SYNC_INTERVAL}s"
            ) from exc

        workflow_state["repository_id"] = repository.id
        logging.info("repository %s in-sync (id=%s)", REPO_NAME, repository.id)

    async def run_generator_definition(
        self,
        client: InfrahubClient,
        name: str,
        branch: str = "main",
    ) -> None:
        """Trigger a ``CoreGeneratorDefinition`` by name and await its task.

        Mirrors ``scripts/run_generator.py``: bootstrap runs the generators
        explicitly so their output lands before artifacts render.

        Args:
            client: Async Infrahub client.
            name: Generator definition name from ``.infrahub.yml``.
            branch: Branch to run the generator on.

        Raises:
            AssertionError: If the definition never appears or the run reports
                no task.
        """

        async def available() -> tuple[bool, Any]:
            definition = await client.get(
                kind="CoreGeneratorDefinition",
                name__value=name,
                branch=branch,
                raise_when_missing=False,
            )
            return definition is not None, definition

        try:
            definition = await self.wait_for(
                available,
                attempts=GENERATOR_DEFINITION_ATTEMPTS,
                interval=GENERATOR_DEFINITION_INTERVAL,
                description=f"generator definition {name!r}",
            )
        except TimeoutError as exc:
            known = [
                d.name.value for d in await client.all("CoreGeneratorDefinition", branch=branch)
            ]
            raise AssertionError(
                f"generator definition {name!r} not found after repository sync.\n"
                f"  definitions present: {known}"
            ) from exc

        mutation = Mutation(
            mutation="CoreGeneratorDefinitionRun",
            input_data={"data": {"id": definition.id}, "wait_until_completion": False},
            query={"ok": None, "task": {"id": None}},
        )
        response = await client.execute_graphql(query=mutation.render(), branch_name=branch)
        task_id = response["CoreGeneratorDefinitionRun"]["task"]["id"]
        logging.info("generator %s started (task=%s)", name, task_id)

        task = await client.task.wait_for_completion(id=task_id, timeout=GENERATOR_TASK_TIMEOUT)
        assert task.state == TaskState.COMPLETED, (
            f"generator {name!r} did not complete.\n"
            f"  task id: {task_id}\n"
            f"  state: {task.state}\n"
            f"  log tail:\n{self.task_log_tail(task)}"
        )
        logging.info("generator %s completed", name)

    @pytest.mark.order(6)
    @pytest.mark.dependency(name="l3vpn_generator", depends=["repository"])
    async def test_06_run_l3vpn_generator(self, async_client_main: InfrahubClient) -> None:
        """Run ``generate_l3vpn`` over the seeded services on main."""
        await self.run_generator_definition(async_client_main, "generate_l3vpn")

    @pytest.mark.order(7)
    @pytest.mark.dependency(name="sdwan_generator", depends=["l3vpn_generator"])
    async def test_07_run_sdwan_generator(self, async_client_main: InfrahubClient) -> None:
        """Run ``generate_sdwan`` over the seeded services on main."""
        await self.run_generator_definition(async_client_main, "generate_sdwan")

    @pytest.mark.order(8)
    @pytest.mark.dependency(name="triggers", depends=["sdwan_generator"])
    def test_08_load_event_triggers(self, client_main: InfrahubClientSync) -> None:
        """Load the generator action and group trigger rule.

        Loaded after repository sync because the trigger references the
        ``generate_l3vpn`` definition, which only exists once ``.infrahub.yml``
        has been imported.
        """
        result = self.execute_command(
            "infrahubctl object load objects/events/00_triggers.yml",
            address=client_main.config.address,
        )
        self.assert_command_ok(result, "event trigger load")

    @pytest.mark.order(9)
    @pytest.mark.dependency(name="generated_data", depends=["l3vpn_generator"])
    async def test_09_verify_generated_l3vpn_data(self, async_client_main: InfrahubClient) -> None:
        """Verify the generator materialised VRF, addressing, and BGP sessions."""
        client = async_client_main

        vrf = await client.get(
            kind="IpamVRF",
            name__value=BOOTSTRAP_VPN,
            raise_when_missing=False,
        )
        assert vrf, (
            f"generator did not create the VRF for {BOOTSTRAP_VPN}.\n"
            f"  VRFs present: {[v.name.value for v in await client.all(kind='IpamVRF')]}"
        )
        assert vrf.vrf_rd.value, f"VRF {BOOTSTRAP_VPN} has no route distinguisher"
        logging.info("VRF %s present with RD %s", BOOTSTRAP_VPN, vrf.vrf_rd.value)

        vpn = await client.get(kind="ServiceL3Vpn", name__value=BOOTSTRAP_VPN)
        assert vpn.status.value == "active", (
            f"{BOOTSTRAP_VPN} should be active once the generator has run, got {vpn.status.value!r}"
        )

        sites = await client.all(kind="ServiceL3VpnSite")
        assert sites, "no ServiceL3VpnSite rows found after the generator ran"
        for site in sites:
            assert site.status.value == "active", (
                f"site {site.name.value} left in status {site.status.value!r}"
            )
        logging.info("%d L3VPN sites materialised", len(sites))

        # Derived from the sites the generator actually materialised rather than
        # naming one. Hardcoding a site name couples this assertion to whichever
        # dataset happens to be loaded -- it read "london", which the financial
        # overlay renamed to "trading-london" when it grew a second customer VPN.
        # Every eBGP site must have its PE-side session; that is the invariant.
        all_sessions = await client.all(kind="RoutingBGPSession")
        described = {s.description.value for s in all_sessions}
        ebgp_sites = [s for s in sites if s.routing_protocol.value == "ebgp"]
        assert ebgp_sites, "dataset defines no eBGP L3VPN site to verify"
        for site in ebgp_sites:
            vpn_name = site.l3vpn.peer.name.value
            expected = f"L3VPN PE-CE {vpn_name} {site.name.value}"
            assert expected in described, (
                f"generator did not create the PE-CE eBGP session for {site.name.value}.\n"
                f"  expected: {expected}\n"
                f"  sessions present: {sorted(described)}"
            )

    @pytest.mark.order(10)
    @pytest.mark.dependency(name="artifacts", depends=["generated_data"])
    async def test_10_regenerate_artifacts(
        self,
        client_main: InfrahubClientSync,
        async_client_main: InfrahubClient,
    ) -> None:
        """Regenerate every artifact and require all of them to reach Ready.

        Runs the same script bootstrap does, so a transform or template that
        breaks on the new release fails here rather than silently leaving
        artifacts in ``Error``.
        """
        result = self.execute_command(
            "python scripts/regenerate_artifacts.py",
            address=client_main.config.address,
        )
        self.assert_command_ok(result, "artifact regeneration")

        async def all_ready() -> tuple[bool, list[Any]]:
            artifacts = await async_client_main.all(kind="CoreArtifact")
            pending = [a for a in artifacts if a.status.value != "Ready"]
            return bool(artifacts) and not pending, artifacts

        try:
            artifacts = await self.wait_for(
                all_ready,
                attempts=ARTIFACT_ATTEMPTS,
                interval=ARTIFACT_INTERVAL,
                description="every CoreArtifact to reach Ready",
            )
        except TimeoutError as exc:
            current = await async_client_main.all(kind="CoreArtifact")
            stuck = [(a.name.value, a.status.value) for a in current if a.status.value != "Ready"]
            raise AssertionError(f"artifacts did not all reach Ready.\n  stuck: {stuck}") from exc

        logging.info("%d artifacts Ready", len(artifacts))

    # ----------------------------------------------------------- service request

    @pytest.mark.order(11)
    @pytest.mark.dependency(name="drop_sidecar_check", depends=["artifacts"])
    async def test_11_drop_sidecar_dependent_check(self, async_client_main: InfrahubClient) -> None:
        """Remove the check that needs the batfish-runner sidecar.

        Runs before the branch is created so the branch never inherits the
        definition. See ``SIDECAR_DEPENDENT_CHECK`` for why this is necessary;
        every other check still runs and is required to pass.
        """
        definition = await async_client_main.get(
            kind="CoreCheckDefinition",
            name__value=SIDECAR_DEPENDENT_CHECK,
            branch="main",
            raise_when_missing=False,
        )
        assert definition, (
            f"check definition {SIDECAR_DEPENDENT_CHECK!r} not found, so the "
            "repository import may have changed.\n"
            "  definitions present: "
            f"{[d.name.value for d in await async_client_main.all('CoreCheckDefinition')]}"
        )

        await definition.delete()
        logging.info("dropped check definition %s for this run", SIDECAR_DEPENDENT_CHECK)

        remaining = await async_client_main.get(
            kind="CoreCheckDefinition",
            name__value=SIDECAR_DEPENDENT_CHECK,
            branch="main",
            raise_when_missing=False,
        )
        assert remaining is None, f"{SIDECAR_DEPENDENT_CHECK} still present after delete"

    @pytest.mark.order(12)
    @pytest.mark.dependency(name="branch", depends=["drop_sidecar_check", "triggers"])
    def test_12_create_service_branch(
        self,
        client_main: InfrahubClientSync,
        default_branch: str,
    ) -> None:
        """Create the branch the service request is staged on."""
        if default_branch not in client_main.branch.all():
            client_main.branch.create(default_branch, wait_until_completion=True)

        branches = client_main.branch.all()
        assert default_branch in branches, (
            f"branch {default_branch} was not created.\n  branches: {list(branches)}"
        )

    @pytest.mark.order(13)
    @pytest.mark.dependency(name="service_request", depends=["branch"])
    def test_13_load_service_request(
        self,
        client_main: InfrahubClientSync,
        default_branch: str,
    ) -> None:
        """Load a new ``ServiceL3Vpn`` onto the branch, as the catalog would."""
        result = self.execute_command(
            f"infrahubctl object load {BRANCH_REQUEST_FILE} --branch {default_branch}",
            address=client_main.config.address,
        )
        self.assert_command_ok(result, "service request load")

    @pytest.mark.order(14)
    @pytest.mark.dependency(name="triggered_generator", depends=["service_request"])
    async def test_14_group_trigger_runs_generator(
        self,
        async_client_main: InfrahubClient,
        default_branch: str,
    ) -> None:
        """Verify the group trigger fired the generator on the branch.

        Adding the service to the ``l3vpns`` group should fire the
        ``CoreGroupTriggerRule`` from ``objects/events/00_triggers.yml``, which
        runs ``generate_l3vpn`` on the branch. Its VRF appearing is the proof.
        """
        client = async_client_main
        client.default_branch = default_branch

        async def vrf_present() -> tuple[bool, Any]:
            vrf = await client.get(
                kind="IpamVRF",
                name__value=BRANCH_VPN,
                branch=default_branch,
                raise_when_missing=False,
            )
            return vrf is not None, vrf

        try:
            vrf = await self.wait_for(
                vrf_present,
                attempts=TRIGGERED_GENERATOR_ATTEMPTS,
                interval=TRIGGERED_GENERATOR_INTERVAL,
                description=f"group trigger to generate the VRF for {BRANCH_VPN}",
            )
        except TimeoutError as exc:
            vrfs = [v.name.value for v in await client.all(kind="IpamVRF", branch=default_branch)]
            actions = [
                a.name.value for a in await client.all(kind="CoreGeneratorAction", branch="main")
            ]
            rules = [
                r.name.value for r in await client.all(kind="CoreGroupTriggerRule", branch="main")
            ]
            raise AssertionError(
                f"group trigger never ran generate_l3vpn on {default_branch}.\n"
                f"  VRFs on branch: {vrfs}\n"
                f"  generator actions: {actions}\n"
                f"  group trigger rules: {rules}\n"
                f"  waited: {TRIGGERED_GENERATOR_ATTEMPTS * TRIGGERED_GENERATOR_INTERVAL}s"
            ) from exc

        logging.info("group trigger produced VRF %s (RD %s)", BRANCH_VPN, vrf.vrf_rd.value)

        sites = await client.filters(
            kind="ServiceL3VpnSite",
            l3vpn__name__value=BRANCH_VPN,
            branch=default_branch,
        )
        assert len(sites) == 2, (
            f"expected 2 sites on {BRANCH_VPN}, got {len(sites)}: {[s.name.value for s in sites]}"
        )

    @pytest.mark.order(15)
    @pytest.mark.dependency(name="diff", depends=["triggered_generator"])
    def test_15_update_diff(
        self,
        client_main: InfrahubClientSync,
        default_branch: str,
    ) -> None:
        """Compute the branch diff ahead of the proposed change."""
        mutation = Mutation(
            mutation="DiffUpdate",
            input_data={
                "data": {
                    "name": f"diff-for-{default_branch}",
                    "branch": default_branch,
                    "wait_for_completion": False,
                }
            },
            query={"ok": None, "task": {"id": None}},
        )

        response = client_main.execute_graphql(query=mutation.render())
        task_id = response["DiffUpdate"]["task"]["id"]
        task = client_main.task.wait_for_completion(id=task_id, timeout=DIFF_TASK_TIMEOUT)

        assert task.state == TaskState.COMPLETED, (
            f"diff did not complete.\n  branch: {default_branch}\n"
            f"  task id: {task_id}\n  state: {task.state}"
        )

    @pytest.mark.order(16)
    @pytest.mark.dependency(name="proposed_change", depends=["diff"])
    def test_16_create_proposed_change(
        self,
        client_main: InfrahubClientSync,
        default_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Open a proposed change and let the full validation pipeline run.

        This is where the repository's checks, transforms, and artifact
        definitions all execute server-side, so it is the most sensitive part
        of the suite to an Infrahub upgrade.
        """
        pc_name = f"Add {BRANCH_VPN} ({default_branch})"
        mutation = Mutation(
            mutation="CoreProposedChangeCreate",
            input_data={
                "data": {
                    "name": {"value": pc_name},
                    "source_branch": {"value": default_branch},
                    "destination_branch": {"value": "main"},
                }
            },
            query={"ok": None, "object": {"id": None}},
        )

        response = client_main.execute_graphql(query=mutation.render())
        pc_id = response["CoreProposedChangeCreate"]["object"]["id"]
        workflow_state["pc_id"] = pc_id
        workflow_state["pc_name"] = pc_name
        logging.info("proposed change %s created (id=%s)", pc_name, pc_id)

        validations: list[Any] = []
        settled_count = -1
        for attempt in range(1, VALIDATION_ATTEMPTS + 1):
            pc = client_main.get(
                "CoreProposedChange",
                id=pc_id,
                include=["validations"],
                exclude=["reviewers", "approved_by", "created_by"],
                prefetch_relationships=True,
                populate_store=True,
            )
            peers = [v.peer for v in pc.validations.peers]
            complete = bool(peers) and all(peer.state.value == "completed" for peer in peers)

            # The pipeline registers validators progressively, so "every known
            # validator is complete" is briefly true while more are still being
            # enqueued. Only accept it once the count has stopped moving.
            if complete and len(peers) == settled_count:
                validations = peers
                break
            settled_count = len(peers) if complete else -1

            logging.info(
                "waiting for validations (%d/%d), %d known, %d complete",
                attempt,
                VALIDATION_ATTEMPTS,
                len(peers),
                sum(1 for peer in peers if peer.state.value == "completed"),
            )
            time.sleep(VALIDATION_INTERVAL)

        assert validations, (
            f"proposed-change validations never completed.\n"
            f"  proposed change: {pc_id}\n"
            f"  waited: {VALIDATION_ATTEMPTS * VALIDATION_INTERVAL}s"
        )

        failed: list[str] = []
        for validation in validations:
            # CoreValidator carries `label`, not `name`.
            name = validation.label.value or validation.id
            conclusion = validation.conclusion.value
            logging.info("validation %s -> %s", name, conclusion)
            if conclusion != "success":
                failed.append(f"{name} -> {conclusion}")

        assert not failed, (
            "proposed-change validations did not all succeed.\n"
            f"  failing: {failed}\n"
            f"  total validators: {len(validations)}"
        )
        logging.info("all %d validators concluded success", len(validations))

    @pytest.mark.order(17)
    @pytest.mark.dependency(name="merge", depends=["proposed_change"])
    def test_17_merge_proposed_change(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge the proposed change into main."""
        pc_id = workflow_state["pc_id"]

        mutation = Mutation(
            mutation="CoreProposedChangeMerge",
            input_data={"data": {"id": pc_id}, "wait_until_completion": False},
            query={"ok": None, "task": {"id": None}},
        )
        response = client_main.execute_graphql(query=mutation.render())
        task_id = response["CoreProposedChangeMerge"]["task"]["id"]
        task = client_main.task.wait_for_completion(id=task_id, timeout=MERGE_TASK_TIMEOUT)
        logging.info("merge task %s finished in state %s", task_id, task.state)
        logging.info("merge task log tail:\n%s", self.task_log_tail(task))

        # The proposed change's own state is authoritative: the merge task can
        # report a non-COMPLETED state from post-merge bookkeeping even when
        # the branch itself merged cleanly.
        pc = client_main.get("CoreProposedChange", id=pc_id)
        state = pc.state.value
        assert state in ("merged", "closed"), (
            f"proposed change did not merge.\n"
            f"  proposed change: {pc_id}\n"
            f"  task id: {task_id}\n"
            f"  task state: {task.state}\n"
            f"  proposed change state: {state}\n"
            f"  task log tail:\n{self.task_log_tail(task)}"
        )

    @pytest.mark.order(18)
    @pytest.mark.dependency(name="verify_main", depends=["merge"])
    async def test_18_verify_merged_to_main(self, async_client_main: InfrahubClient) -> None:
        """Verify the new service and its generated data landed on main."""
        client = async_client_main
        client.default_branch = "main"
        await asyncio.sleep(MERGE_PROPAGATION_DELAY)

        vpn = await client.get(
            kind="ServiceL3Vpn",
            name__value=BRANCH_VPN,
            branch="main",
            raise_when_missing=False,
        )
        assert vpn, (
            f"{BRANCH_VPN} not on main after merge.\n"
            f"  services on main: "
            f"{[v.name.value for v in await client.all(kind='ServiceL3Vpn', branch='main')]}"
        )

        vrf = await client.get(
            kind="IpamVRF",
            name__value=BRANCH_VPN,
            branch="main",
            raise_when_missing=False,
        )
        assert vrf, (
            f"VRF for {BRANCH_VPN} not on main after merge; the generator output "
            "did not survive the merge"
        )

        logging.info("%s and its VRF verified on main", BRANCH_VPN)
