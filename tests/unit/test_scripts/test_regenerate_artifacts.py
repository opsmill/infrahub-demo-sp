"""Unit tests for `scripts/regenerate_artifacts.py`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts import regenerate_artifacts


def _definition(id_: str, name: str) -> MagicMock:
    """Mock a CoreArtifactDefinition row with the .id and .name.value the script reads."""
    d = MagicMock()
    d.id = id_
    d.name.value = name
    return d


def _artifact(id_: str, name: str, status: str) -> MagicMock:
    """Mock a CoreArtifact row with the .id, .name.value, .status.value shape."""
    a = MagicMock()
    a.id = id_
    a.name.value = name
    a.status.value = status
    return a


def test_no_definitions_returns_zero(capsys) -> None:
    """A repo with no artifact definitions should exit successfully without polling."""
    client = MagicMock()
    client.all = MagicMock(return_value=[])
    with patch.object(regenerate_artifacts, "InfrahubClientSync", return_value=client):
        rc = regenerate_artifacts.main()
    assert rc == 0
    assert "nothing to regenerate" in capsys.readouterr().out.lower()


def test_trigger_generate_posts_through_the_client() -> None:
    """The POST goes through the SDK client, which adds credentials and a timeout.

    A bare urllib call had neither: no timeout, so one slow render blocked the
    caller, and a `KeyError` when INFRAHUB_API_TOKEN was not exported.
    """
    client = MagicMock()
    client.address = "http://localhost:8000"
    regenerate_artifacts._trigger_generate(client, "def-1", "main")
    client._post.assert_called_once_with(
        url="http://localhost:8000/api/artifact/generate/def-1?branch=main",
        payload={"nodes": []},
    )
    client._post.return_value.raise_for_status.assert_called_once_with()


def test_delete_existing_artifacts_calls_graphql_delete_per_row() -> None:
    """One delete mutation per artifact found under the definition."""
    client = MagicMock()
    client.filters = MagicMock(
        return_value=[_artifact("a-1", "art-1", "Ready"), _artifact("a-2", "art-2", "Error")]
    )
    deleted = regenerate_artifacts._delete_existing_artifacts(client, "def-99")
    assert deleted == 2
    # First positional kwarg is the GraphQL mutation; variables carry the id.
    calls = client.execute_graphql.call_args_list
    assert len(calls) == 2
    deleted_ids = {c.kwargs["variables"]["id"] for c in calls}
    assert deleted_ids == {"a-1", "a-2"}
    # Filter must be by definition id, not by name (that bug shipped once already).
    client.filters.assert_called_once_with(kind="CoreArtifact", definition__ids=["def-99"])


def test_main_happy_path_returns_zero_when_all_ready(capsys) -> None:
    """Two definitions, delete stale, trigger, all artifacts Ready on first poll → rc 0."""
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.default_branch = "main"
    defs = [_definition("d-1", "pe-arista-eos"), _definition("d-2", "pe-cisco-iosxr")]
    # `.all()` is called for definitions first, then for artifacts during the poll.
    client.all = MagicMock(
        side_effect=[
            defs,
            [
                _artifact("a-1", "pe-arista-eos", "Ready"),
                _artifact("a-2", "pe-cisco-iosxr", "Ready"),
            ],
        ]
    )
    client.filters = MagicMock(
        side_effect=[
            [_artifact("a-1", "pe-arista-eos", "Ready")],
            [_artifact("a-2", "pe-cisco-iosxr", "Ready")],
        ]
    )

    with (
        patch.object(regenerate_artifacts, "InfrahubClientSync", return_value=client),
        patch("scripts.regenerate_artifacts.time.sleep"),
        # First monotonic call sets the deadline; the loop polls once and finds Ready.
        patch("scripts.regenerate_artifacts.time.monotonic", side_effect=[0.0, 0.1, 0.2]),
    ):
        rc = regenerate_artifacts.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "queued: pe-arista-eos" in out
    assert "queued: pe-cisco-iosxr" in out
    assert "All 2 artifacts Ready" in out


def test_main_times_out_when_artifact_stuck_non_ready(capsys) -> None:
    """If polling never sees all-Ready before the deadline, exit non-zero with detail."""
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.default_branch = "main"
    defs = [_definition("d-1", "pe-arista-eos")]
    error_art = _artifact("a-1", "pe-arista-eos", "Error")
    # First .all() returns definitions; every subsequent .all() returns stuck artifacts.
    client.all = MagicMock(side_effect=[defs] + [[error_art]] * 10)
    client.filters = MagicMock(return_value=[error_art])

    # monotonic: baseline=0 → +0.5 → past the 180s deadline. After exhaustion
    # return the same large value so the loop exits cleanly.
    monotonic_values = iter([0.0, 0.5, 999.0])

    with (
        patch.object(regenerate_artifacts, "InfrahubClientSync", return_value=client),
        patch("scripts.regenerate_artifacts.time.sleep"),
        patch(
            "scripts.regenerate_artifacts.time.monotonic",
            side_effect=lambda: next(monotonic_values, 1_000_000.0),
        ),
    ):
        rc = regenerate_artifacts.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "Timed out" in err
    assert "pe-arista-eos" in err


def test_trigger_generate_names_the_branch_in_the_url() -> None:
    """The generate endpoint renders on main unless `?branch=` says otherwise.

    The client's default branch never reaches this call. Without the
    parameter, a run against a proposed change's branch deleted that branch's
    artifacts and then rendered main's. Branch names carry `/`, so it is encoded.
    """
    client = MagicMock()
    client.address = "http://localhost:8000"
    regenerate_artifacts._trigger_generate(client, "def-1", "backbone/add-pe-pe-09-1234")
    assert client._post.call_args.kwargs["url"] == (
        "http://localhost:8000/api/artifact/generate/def-1?branch=backbone%2Fadd-pe-pe-09-1234"
    )


def test_main_branch_flag_reads_and_renders_on_that_branch() -> None:
    """`--branch` sets the client's default branch and the branch on every generate call."""
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.all = MagicMock(
        side_effect=[[_definition("d-1", "pe-arista-eos")], [_artifact("a-1", "x", "Ready")]]
    )
    client.filters = MagicMock(return_value=[_artifact("a-1", "x", "Ready")])

    with (
        patch.object(regenerate_artifacts, "InfrahubClientSync", return_value=client) as ctor,
        patch("scripts.regenerate_artifacts.time.sleep"),
        patch("scripts.regenerate_artifacts.time.monotonic", side_effect=[0.0, 0.1, 0.2]),
    ):
        rc = regenerate_artifacts.main(["--branch", "feature-x"])

    assert rc == 0
    assert ctor.call_args.kwargs["config"].default_branch == "feature-x"
    assert client._post.call_args.kwargs["url"].endswith(
        "/api/artifact/generate/d-1?branch=feature-x"
    )


def test_main_without_branch_flag_uses_the_client_default_branch() -> None:
    """With no `--branch`, the URL follows the client's default branch.

    That default is main unless INFRAHUB_DEFAULT_BRANCH is exported, and an
    exported branch has to reach the generate call too.
    """
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.default_branch = "from-env"
    client.all = MagicMock(
        side_effect=[[_definition("d-1", "pe-arista-eos")], [_artifact("a-1", "x", "Ready")]]
    )
    client.filters = MagicMock(return_value=[_artifact("a-1", "x", "Ready")])

    with (
        patch.object(regenerate_artifacts, "InfrahubClientSync", return_value=client) as ctor,
        patch("scripts.regenerate_artifacts.time.sleep"),
        patch("scripts.regenerate_artifacts.time.monotonic", side_effect=[0.0, 0.1, 0.2]),
    ):
        regenerate_artifacts.main()

    ctor.assert_called_once_with()
    assert client._post.call_args.kwargs["url"].endswith(
        "/api/artifact/generate/d-1?branch=from-env"
    )
