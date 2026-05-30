"""Unit tests for `scripts/regenerate_artifacts.py`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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


@pytest.fixture
def fake_response() -> MagicMock:
    """A context-manager-friendly response object for urllib.urlopen."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=b"")
    return resp


def test_no_definitions_returns_zero(capsys) -> None:
    """A repo with no artifact definitions should exit successfully without polling."""
    client = MagicMock()
    client.all = MagicMock(return_value=[])
    with patch.object(regenerate_artifacts, "InfrahubClientSync", return_value=client):
        rc = regenerate_artifacts.main()
    assert rc == 0
    assert "nothing to regenerate" in capsys.readouterr().out.lower()


def test_trigger_generate_posts_with_api_key(fake_response: MagicMock) -> None:
    """The trigger request must carry X-INFRAHUB-KEY so Infrahub authorizes it."""
    client = MagicMock()
    client.address = "http://localhost:8000"
    with (
        patch.dict("os.environ", {"INFRAHUB_API_TOKEN": "test-token"}, clear=False),
        patch(
            "scripts.regenerate_artifacts.urllib.request.urlopen",
            return_value=fake_response,
        ) as urlopen,
    ):
        regenerate_artifacts._trigger_generate(client, "def-1")
    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("/api/artifact/generate/def-1")
    assert request.headers["X-infrahub-key"] == "test-token"
    assert request.get_method() == "POST"


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


def test_main_happy_path_returns_zero_when_all_ready(fake_response: MagicMock, capsys) -> None:
    """Two definitions, delete stale, trigger, all artifacts Ready on first poll → rc 0."""
    client = MagicMock()
    client.address = "http://localhost:8000"
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
        patch.dict("os.environ", {"INFRAHUB_API_TOKEN": "tok"}, clear=False),
        patch("scripts.regenerate_artifacts.urllib.request.urlopen", return_value=fake_response),
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


def test_main_times_out_when_artifact_stuck_non_ready(fake_response: MagicMock, capsys) -> None:
    """If polling never sees all-Ready before the deadline, exit non-zero with detail."""
    client = MagicMock()
    client.address = "http://localhost:8000"
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
        patch.dict("os.environ", {"INFRAHUB_API_TOKEN": "tok"}, clear=False),
        patch("scripts.regenerate_artifacts.urllib.request.urlopen", return_value=fake_response),
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
