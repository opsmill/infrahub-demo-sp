# pyright: reportAttributeAccessIssue=false
"""End-to-end demo workflow against a containerised Infrahub.

Two halves, following the README: bootstrap (schemas, menu, objects, protocol export,
repository registration, generators, event triggers, artifacts) and a service request
(branch, load a ServiceL3Vpn, group trigger, diff, proposed change, merge, verify main).

This is the suite the Infrahub version-bump automation runs against a new release.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.task.models import TaskState
from infrahub_sdk.testing.repository import GitRepo

from .conftest import ADMIN_TOKEN, PROJECT_DIRECTORY, TestInfrahubDockerWithClient

REPO_NAME = "infrahub-demo-sp"
SERVICE_BRANCH = "integration-add-l3vpn"
BOOTSTRAP_VPN = "trading-floor-vpn"
BRANCH_VPN = "integration-test-vpn"
BRANCH_REQUEST_FILE = "tests/integration/data/l3vpn_branch_request.yml"
PE_BRANCH = "integration-add-pe"
BRANCH_PE = "pe-09"
PE_UPLINKS = ("pe-02", "pe-07")
PE_REQUEST_FILE = "tests/integration/data/pe_branch_request.yml"
BACKBONE = "mpls-backbone-1"

# Generous: a cold container pulls images, syncs the repository, and renders artifacts.
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

# This check needs the batfish-runner sidecar, which the testcontainers stack has no way
# to provide, so it would always fail and block the merge. Dropped before branching.
SIDECAR_DEPENDENT_CHECK = "batfish_backbone"

T = TypeVar("T")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestSpDemoWorkflow(TestInfrahubDockerWithClient):
    """The full service-provider demo workflow, start to finish."""

    @pytest.fixture(scope="class")
    def default_branch(self) -> str:
        """Branch the service request is staged on."""
        return SERVICE_BRANCH

    @pytest.fixture(scope="class")
    def workflow_state(self) -> dict[str, Any]:
        """IDs and task handles shared between ordered tests."""
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
        """Export Python protocols from the live schema, to a temp path not the tracked file."""
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

        The sync instantiates the definitions in ``.infrahub.yml`` that later tests need.
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

        Mirrors ``scripts/run_generator.py``.

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

        After repository sync: the trigger references a definition from ``.infrahub.yml``.
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

        # Assert on the VPN's session prefix, not a hard-coded site name: sites are
        # dataset-defined, and renaming one is not a generator regression.
        prefix = f"L3VPN PE-CE {BOOTSTRAP_VPN} "
        all_sessions = await client.all(kind="RoutingBGPSession")
        sessions = [s for s in all_sessions if (s.description.value or "").startswith(prefix)]
        assert sessions, (
            f"generator did not create any PE-CE eBGP session for {BOOTSTRAP_VPN}.\n"
            f"  sessions present: {[s.description.value for s in all_sessions]}"
        )

    @pytest.mark.order(10)
    @pytest.mark.dependency(name="artifacts", depends=["generated_data"])
    async def test_10_regenerate_artifacts(
        self,
        client_main: InfrahubClientSync,
        async_client_main: InfrahubClient,
    ) -> None:
        """Regenerate every artifact and require all of them to reach Ready."""
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

        Before the branch exists, so it never inherits the definition. See
        ``SIDECAR_DEPENDENT_CHECK``; every other check still has to pass.
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

        Adding the service to the ``l3vpns`` group runs ``generate_l3vpn``; its VRF is the proof.
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

    # Deferred to the advisory tier, not fixed: `generate_l3vpn` creates the
    # PE-side /30 of a site twice on a branch, and the Schema Integrity
    # validator correctly rejects the resulting `IpamIPAddress` uniqueness
    # violation. That blocks every proposed change opened from a generated
    # service, so this test cannot pass in the blocking tier until the
    # generator is fixed. See issue #111.
    @pytest.mark.extended
    @pytest.mark.order(16)
    @pytest.mark.dependency(name="proposed_change", depends=["diff"])
    def test_16_create_proposed_change(
        self,
        client_main: InfrahubClientSync,
        default_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Open a proposed change and let the full validation pipeline run server-side."""
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

            # Validators register progressively, so "all complete" is briefly true
            # while more are enqueued. Accept it only once the count stops moving.
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

    # Rides the advisory tier with test_16: `merge` depends on
    # `proposed_change`, which cannot conclude while #111 stands.
    @pytest.mark.extended
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

        # The proposed change's own state is authoritative: post-merge bookkeeping
        # can report a non-COMPLETED task even when the branch merged cleanly.
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

    # Rides the advisory tier with test_16: `verify_main` depends on `merge`,
    # the tail of the chain #111 blocks.
    @pytest.mark.extended
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

    # ------------------------------------------------------------ PE onboarding
    #
    # Independent of the L3VPN proposed change above (16 to 18, `extended`): the
    # PE flow never runs generate_l3vpn, so it does not hit issue #111 and its
    # proposed change can merge in the blocking tier.

    @staticmethod
    def _api_headers() -> dict[str, str]:
        """Auth header for the REST endpoints the SDK does not wrap."""
        return {"X-INFRAHUB-KEY": ADMIN_TOKEN}

    async def pe_state(self, client: InfrahubClient, branch: str) -> dict[str, Any]:
        """Read everything the PE assertions look at, in one query on ``branch``.

        Args:
            client: Async Infrahub client.
            branch: Branch to read.

        Returns:
            The raw GraphQL result.
        """
        query = """
        query PeState($name: String!, $claim: String!, $backbone: String!) {
          TopologyPeOnboarding(name__value: $name) {
            edges { node { status { value } device { node { name { value } } } } }
          }
          DcimDevice(name__value: $name) {
            edges {
              node {
                role { value }
                interfaces {
                  edges {
                    node {
                      __typename
                      name { value }
                      role { value }
                      status { value }
                      ... on InterfaceVirtual {
                        ip_addresses { edges { node { address { value } } } }
                      }
                      ... on InterfacePhysical {
                        ip_addresses { edges { node { address { value } } } }
                      }
                    }
                  }
                }
              }
            }
          }
          pe_count: DcimDevice(role__value: "pe") { count }
          RoutingBGPSession(device__name__value: $name, session_type__value: "INTERNAL") { count }
          claimed: InterfacePhysical(description__value: $claim) {
            edges {
              node {
                name { value }
                role { value }
                status { value }
                device { node { name { value } } }
                ip_addresses { edges { node { address { value } } } }
              }
            }
          }
          TopologyMplsBackbone(name__value: $backbone) {
            edges { node { pes { edges { node { name { value } } } } } }
          }
          MplsIsisProcess(device__name__value: $name) { edges { node { net_id { value } } } }
        }
        """
        return await client.execute_graphql(
            query=query,
            variables={"name": BRANCH_PE, "claim": f"To {BRANCH_PE}", "backbone": BACKBONE},
            branch_name=branch,
        )

    @staticmethod
    def _pe_materialised(state: dict[str, Any]) -> bool:
        """The gate the catalog page uses: active, linked, every port and session present."""
        requests = state["TopologyPeOnboarding"]["edges"]
        if not requests:
            return False
        request = requests[0]["node"]
        if request["status"]["value"] != "active" or not request["device"]["node"]:
            return False
        expected_sessions = int(state["pe_count"]["count"]) - 1
        devices = state["DcimDevice"]["edges"]
        if not devices:
            return False
        core_ports = [
            i["node"]
            for i in devices[0]["node"]["interfaces"]["edges"]
            if i["node"]["__typename"] == "InterfacePhysical"
            and i["node"]["role"]["value"] == "core"
        ]
        return (
            len(core_ports) == len(PE_UPLINKS)
            and int(state["RoutingBGPSession"]["count"]) == expected_sessions
        )

    @pytest.mark.order(19)
    @pytest.mark.dependency(name="pe_branch", depends=["diff"])
    def test_19_create_pe_branch(self, client_main: InfrahubClientSync) -> None:
        """Create the branch the onboarding request is staged on."""
        if PE_BRANCH not in client_main.branch.all():
            client_main.branch.create(PE_BRANCH, wait_until_completion=True)
        assert PE_BRANCH in client_main.branch.all()

    @pytest.mark.order(20)
    @pytest.mark.dependency(name="pe_request", depends=["pe_branch"])
    def test_20_load_pe_request(self, client_main: InfrahubClientSync) -> None:
        """Load a ``TopologyPeOnboarding`` onto the branch, as the catalog would."""
        result = self.execute_command(
            f"infrahubctl object load {PE_REQUEST_FILE} --branch {PE_BRANCH}",
            address=client_main.config.address,
        )
        self.assert_command_ok(result, "PE request load")

    @pytest.mark.order(21)
    @pytest.mark.dependency(name="pe_generated", depends=["pe_request"])
    async def test_21_group_trigger_runs_pe_generator(
        self, async_client_main: InfrahubClient
    ) -> None:
        """The group trigger ran generate_pe and it wrote the whole PE."""
        client = async_client_main

        async def materialised() -> tuple[bool, dict[str, Any]]:
            state = await self.pe_state(client, PE_BRANCH)
            return self._pe_materialised(state), state

        try:
            state = await self.wait_for(
                materialised,
                attempts=TRIGGERED_GENERATOR_ATTEMPTS,
                interval=TRIGGERED_GENERATOR_INTERVAL,
                description=f"group trigger to run generate_pe for {BRANCH_PE}",
            )
        except TimeoutError as exc:
            actions = [a.name.value for a in await client.all(kind="CoreGeneratorAction")]
            rules = [r.name.value for r in await client.all(kind="CoreGroupTriggerRule")]
            last = await self.pe_state(client, PE_BRANCH)
            raise AssertionError(
                f"group trigger never completed generate_pe on {PE_BRANCH}.\n"
                f"  generator actions: {actions}\n"
                f"  group trigger rules: {rules}\n"
                f"  last state: {last}\n"
                f"  waited: {TRIGGERED_GENERATOR_ATTEMPTS * TRIGGERED_GENERATOR_INTERVAL}s"
            ) from exc

        # Settle: a sibling run must not still be rewriting what we are about to assert on.
        await asyncio.sleep(TRIGGERED_GENERATOR_INTERVAL)
        state = await self.pe_state(client, PE_BRANCH)
        assert self._pe_materialised(state), f"PE state regressed while settling: {state}"

        device = state["DcimDevice"]["edges"][0]["node"]
        assert device["role"]["value"] == "pe"
        interfaces = [i["node"] for i in device["interfaces"]["edges"]]
        loopbacks = [i for i in interfaces if i["name"]["value"] == "Loopback0"]
        assert len(loopbacks) == 1, f"expected one Loopback0 on {BRANCH_PE}, got {interfaces}"
        loopback = loopbacks[0]["ip_addresses"]["edges"][0]["node"]["address"]["value"]
        assert loopback.startswith("10.0.0.") and loopback.endswith("/32"), loopback

        core = {
            i["name"]["value"]: i["ip_addresses"]["edges"][0]["node"]["address"]["value"]
            for i in interfaces
            if i["__typename"] == "InterfacePhysical" and i["role"]["value"] == "core"
        }
        assert sorted(core) == ["Ethernet1", "Ethernet2"], core
        spare_ports = [
            interface["name"]["value"]
            for interface in interfaces
            if interface["__typename"] == "InterfacePhysical"
            and interface["status"]["value"] == "free"
            and interface["role"]["value"] == "cust"
        ]
        assert sorted(spare_ports) == ["Ethernet3", "Ethernet4"], interfaces

        claimed = {
            c["node"]["device"]["node"]["name"]["value"]: c["node"]
            for c in state["claimed"]["edges"]
        }
        assert sorted(claimed) == sorted(PE_UPLINKS), f"claimed ports: {claimed}"
        local_networks = {str(ipaddress.ip_interface(a).network) for a in core.values()}
        for uplink, port in claimed.items():
            assert port["role"]["value"] == "core" and port["status"]["value"] == "active", port
            address = port["ip_addresses"]["edges"][0]["node"]["address"]["value"]
            network = str(ipaddress.ip_interface(address).network)
            assert network in local_networks, (
                f"{uplink} port {port['name']['value']} ({address}) shares no /31 with {core}"
            )

        backbone_pes = {
            p["node"]["name"]["value"]
            for p in state["TopologyMplsBackbone"]["edges"][0]["node"]["pes"]["edges"]
        }
        assert BRANCH_PE in backbone_pes, backbone_pes

        net_id = state["MplsIsisProcess"]["edges"][0]["node"]["net_id"]["value"]
        octets = loopback.split("/")[0].split(".")
        digits = "".join(f"{int(o):03d}" for o in octets)
        assert net_id == f"49.0001.{digits[0:4]}.{digits[4:8]}.{digits[8:12]}.00", net_id
        logging.info(
            "%s materialised: loopback %s, core %s, NET %s", BRANCH_PE, loopback, core, net_id
        )

    async def _artifact_content(
        self,
        client: InfrahubClient,
        branch: str,
        definition_name: str,
        object_name: str,
    ) -> str | None:
        """Return the rendered artifact for one object on ``branch``, or ``None``."""
        definition = await client.get(kind="CoreArtifactDefinition", name__value=definition_name)
        artifacts = await client.filters(
            kind="CoreArtifact", definition__ids=[definition.id], branch=branch
        )
        for artifact in artifacts:
            if artifact.status.value != "Ready" or not artifact.storage_id.value:
                continue
            await artifact.object.fetch()
            if artifact.object.peer.display_label not in (object_name, None):
                continue
            response = httpx.get(
                f"{client.config.address}/api/storage/object/{artifact.storage_id.value}",
                headers=self._api_headers(),
                timeout=30,
            )
            response.raise_for_status()
            return response.text
        return None

    @pytest.mark.order(22)
    @pytest.mark.dependency(name="pe_artifacts", depends=["pe_generated"])
    async def test_22_pe_artifacts_render_on_branch(
        self, async_client_main: InfrahubClient
    ) -> None:
        """The new PE's config and the lab topology render on the branch.

        Triggered per definition exactly as the catalog page does it.
        """
        client = async_client_main
        for definition_name in ("pe-arista-eos-config", "clab-mpls-topology"):
            definition = await client.get(
                kind="CoreArtifactDefinition", name__value=definition_name
            )
            response = httpx.post(
                f"{client.config.address}/api/artifact/generate/{definition.id}?branch={PE_BRANCH}",
                headers=self._api_headers(),
                timeout=60,
            )
            assert response.status_code == 200, f"{definition_name}: {response.text}"

        async def pe_config() -> tuple[bool, str | None]:
            content = await self._artifact_content(
                client, PE_BRANCH, "pe-arista-eos-config", BRANCH_PE
            )
            return bool(content and f"hostname {BRANCH_PE}" in content), content

        config = await self.wait_for(
            pe_config,
            attempts=ARTIFACT_ATTEMPTS,
            interval=ARTIFACT_INTERVAL,
            description=f"pe-arista-eos artifact for {BRANCH_PE} on {PE_BRANCH}",
        )
        assert config is not None
        assert config.count("peer group RR-MESH") >= 8, config
        assert "net 49.0001." in config and "router isis 1" in config, config
        for uplink in PE_UPLINKS:
            assert f"description To {uplink}" in config, config

        async def topology() -> tuple[bool, str | None]:
            content = await self._artifact_content(
                client, PE_BRANCH, "clab-mpls-topology", BACKBONE
            )
            return bool(content and f"    {BRANCH_PE}:" in content), content

        topo = await self.wait_for(
            topology,
            attempts=ARTIFACT_ATTEMPTS,
            interval=ARTIFACT_INTERVAL,
            description=f"clab topology including {BRANCH_PE} on {PE_BRANCH}",
        )
        assert topo is not None
        pe_links = [
            line for line in topo.splitlines() if "endpoints" in line and f"{BRANCH_PE}:" in line
        ]
        assert len(pe_links) == len(PE_UPLINKS), f"links naming {BRANCH_PE}: {pe_links}"

        # The neighbours' artifacts must carry the new session too: the Arista
        # template reads sessions by device, so a missing reverse row shows up
        # here as an uplink config without the new neighbour.
        state = await self.pe_state(client, PE_BRANCH)
        loopback = next(
            a["node"]["address"]["value"]
            for i in state["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
            if i["node"]["name"]["value"] == "Loopback0"
            for a in i["node"]["ip_addresses"]["edges"]
        ).split("/")[0]
        for uplink in PE_UPLINKS:

            async def uplink_config(name: str = uplink) -> tuple[bool, str | None]:
                content = await self._artifact_content(
                    client, PE_BRANCH, "pe-arista-eos-config", name
                )
                return bool(
                    content and f"neighbor {loopback} peer group RR-MESH" in content
                ), content

            uplink_cfg = await self.wait_for(
                uplink_config,
                attempts=ARTIFACT_ATTEMPTS,
                interval=ARTIFACT_INTERVAL,
                description=f"{uplink} artifact on {PE_BRANCH} to list neighbour {loopback}",
            )
            assert uplink_cfg is not None
            assert f"description To {BRANCH_PE}" in uplink_cfg, uplink_cfg

    def open_proposed_change(
        self, client_main: InfrahubClientSync, branch: str, pc_name: str
    ) -> tuple[str, list[str], set[str]]:
        """Open a proposed change and wait for its validators to settle.

        Returns:
            The proposed change id, the list of ``label -> conclusion`` strings
            for validators that did not conclude ``success``, and the label of
            every validator that ran.
        """
        mutation = Mutation(
            mutation="CoreProposedChangeCreate",
            input_data={
                "data": {
                    "name": {"value": pc_name},
                    "source_branch": {"value": branch},
                    "destination_branch": {"value": "main"},
                }
            },
            query={"ok": None, "object": {"id": None}},
        )
        response = client_main.execute_graphql(query=mutation.render())
        pc_id = response["CoreProposedChangeCreate"]["object"]["id"]
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
            if complete and len(peers) == settled_count:
                validations = peers
                break
            settled_count = len(peers) if complete else -1
            logging.info(
                "waiting for validations of %s (%d/%d), %d known",
                pc_name,
                attempt,
                VALIDATION_ATTEMPTS,
                len(peers),
            )
            time.sleep(VALIDATION_INTERVAL)

        assert validations, f"validations of {pc_name} never completed ({pc_id})"
        failed = []
        for validation in validations:
            name = validation.label.value or validation.id
            conclusion = validation.conclusion.value
            logging.info("validation %s -> %s", name, conclusion)
            if conclusion != "success":
                failed.append(f"{name} -> {conclusion}")
        return pc_id, failed, {validation.label.value for validation in validations}

    @pytest.mark.order(23)
    @pytest.mark.dependency(name="pe_proposed_change", depends=["pe_artifacts"])
    def test_23_create_pe_proposed_change(
        self, client_main: InfrahubClientSync, workflow_state: dict[str, Any]
    ) -> None:
        """Every validator, pe_onboarding and backbone_session_count included, succeeds.

        "Included" is asserted, not assumed: when a repository sync deletes a
        check definition, Infrahub stops creating that check's validator and
        reports no error, so "no validator failed" alone would pass without the
        check ever running (see the rollout section of docs/docs/backbone/add-pe.mdx).
        """
        pc_id, failed, labels = self.open_proposed_change(
            client_main, PE_BRANCH, f"Add PE {BRANCH_PE}"
        )
        workflow_state["pe_pc_id"] = pc_id
        assert not failed, f"proposed-change validations did not all succeed: {failed}"
        for check in ("Check: pe_onboarding", "Check: backbone_session_count"):
            assert check in labels, f"{check} did not run; validators that ran: {sorted(labels)}"

    @pytest.mark.order(24)
    @pytest.mark.dependency(name="pe_merge", depends=["pe_proposed_change"])
    def test_24_merge_pe_proposed_change(
        self, client_main: InfrahubClientSync, workflow_state: dict[str, Any]
    ) -> None:
        """Merge the PE proposed change into main."""
        pc_id = workflow_state["pe_pc_id"]
        mutation = Mutation(
            mutation="CoreProposedChangeMerge",
            input_data={"data": {"id": pc_id}, "wait_until_completion": False},
            query={"ok": None, "task": {"id": None}},
        )
        response = client_main.execute_graphql(query=mutation.render())
        task_id = response["CoreProposedChangeMerge"]["task"]["id"]
        task = client_main.task.wait_for_completion(id=task_id, timeout=MERGE_TASK_TIMEOUT)
        logging.info("PE merge task %s finished in state %s", task_id, task.state)
        pc = client_main.get("CoreProposedChange", id=pc_id)
        assert pc.state.value in ("merged", "closed"), (
            f"PE proposed change did not merge: state {pc.state.value}, task {task.state}\n"
            f"{self.task_log_tail(task)}"
        )

    @pytest.mark.order(25)
    @pytest.mark.dependency(name="pe_on_main", depends=["pe_merge"])
    async def test_25_verify_pe_on_main(self, async_client_main: InfrahubClient) -> None:
        """The new PE, its mesh and the neighbours' new sessions all landed on main."""
        client = async_client_main
        await asyncio.sleep(MERGE_PROPAGATION_DELAY)
        state = await self.pe_state(client, "main")
        assert self._pe_materialised(state), f"{BRANCH_PE} not fully on main: {state}"
        pe_count = int(state["pe_count"]["count"])
        for uplink in PE_UPLINKS:
            sessions = await client.filters(
                kind="RoutingBGPSession",
                device__name__value=uplink,
                session_type__value="INTERNAL",
                branch="main",
            )
            assert len(sessions) == pe_count - 1, (
                f"{uplink} has {len(sessions)} iBGP sessions on main, expected {pe_count - 1}"
            )

        # The artifacts the lab deploys from are the ones on main. Re-render them
        # the way `invoke lab.deploy` does and require the merged PE to be in them.
        for definition_name in ("pe-arista-eos-config", "clab-mpls-topology"):
            definition = await client.get(
                kind="CoreArtifactDefinition", name__value=definition_name
            )
            response = httpx.post(
                f"{client.config.address}/api/artifact/generate/{definition.id}",
                headers=self._api_headers(),
                timeout=60,
            )
            assert response.status_code == 200, f"{definition_name} on main: {response.text}"

        async def main_pe_config() -> tuple[bool, str | None]:
            content = await self._artifact_content(
                client, "main", "pe-arista-eos-config", BRANCH_PE
            )
            return bool(content and f"hostname {BRANCH_PE}" in content), content

        config = await self.wait_for(
            main_pe_config,
            attempts=ARTIFACT_ATTEMPTS,
            interval=ARTIFACT_INTERVAL,
            description=f"pe-arista-eos artifact for {BRANCH_PE} on main after merge",
        )
        assert config is not None and config.count("peer group RR-MESH") >= pe_count - 1, config

        async def main_topology() -> tuple[bool, str | None]:
            content = await self._artifact_content(client, "main", "clab-mpls-topology", BACKBONE)
            return bool(content and f"    {BRANCH_PE}:" in content), content

        topo = await self.wait_for(
            main_topology,
            attempts=ARTIFACT_ATTEMPTS,
            interval=ARTIFACT_INTERVAL,
            description=f"clab topology on main to include {BRANCH_PE} after merge",
        )
        assert topo is not None
        pe_links = [
            line for line in topo.splitlines() if "endpoints" in line and f"{BRANCH_PE}:" in line
        ]
        assert len(pe_links) == len(PE_UPLINKS), f"links naming {BRANCH_PE} on main: {pe_links}"
        logging.info("%s verified on main with %d PEs in the backbone", BRANCH_PE, pe_count)
