"""Unit tests for `scripts/run_generator.py`."""

from __future__ import annotations

import http.server
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest
from infrahub_sdk import Config, InfrahubClientSync
from infrahub_sdk.exceptions import (
    AuthenticationError,
    GraphQLError,
    JsonDecodeError,
    NodeNotFoundError,
    RateLimitError,
    ServerNotReachableError,
    ServerNotResponsiveError,
)

from scripts import run_generator


def test_wait_for_generator_returns_on_first_hit() -> None:
    """When the generator is already registered, the first .get() call returns it."""
    client = MagicMock()
    gen = MagicMock(id="gen-1")
    client.get = MagicMock(return_value=gen)

    with patch("scripts.run_generator.time.sleep") as sleep:
        result = run_generator._wait_for_generator(client, "generate_l3vpn")

    assert result is gen
    client.get.assert_called_once_with(kind="CoreGeneratorDefinition", name__value="generate_l3vpn")
    sleep.assert_not_called()


def test_wait_for_generator_polls_until_available() -> None:
    """If the first lookup raises NodeNotFoundError, poll until it appears."""
    client = MagicMock()
    gen = MagicMock(id="gen-2")
    client.get = MagicMock(
        side_effect=[
            NodeNotFoundError(
                branch_name="main", node_type="CoreGeneratorDefinition", identifier={}
            ),
            NodeNotFoundError(
                branch_name="main", node_type="CoreGeneratorDefinition", identifier={}
            ),
            gen,
        ]
    )

    with (
        patch("scripts.run_generator.time.sleep") as sleep,
        patch("scripts.run_generator.time.monotonic", side_effect=[0.0, 1.0, 2.0]),
    ):
        result = run_generator._wait_for_generator(client, "generate_sdwan")

    assert result is gen
    assert client.get.call_count == 3
    assert sleep.call_count == 2


def test_wait_for_generator_times_out_with_helpful_message() -> None:
    """Past the deadline with no result → TimeoutError with sync-stuck hint."""
    client = MagicMock()
    client.get = MagicMock(
        side_effect=NodeNotFoundError(
            branch_name="main", node_type="CoreGeneratorDefinition", identifier={}
        )
    )
    # monotonic: baseline → past deadline on the very next check.
    monotonic_values = iter([0.0, 999_999.0])

    with (
        patch("scripts.run_generator.time.sleep"),
        patch(
            "scripts.run_generator.time.monotonic",
            side_effect=lambda: next(monotonic_values, 1_000_000.0),
        ),
        pytest.raises(TimeoutError, match="CoreRepository sync stuck"),
    ):
        run_generator._wait_for_generator(client, "generate_l3vpn")


def _graphql(*task_states: str, ok: bool = True) -> MagicMock:
    """Mock ``execute_graphql``: the run mutation, then one task query per state."""
    started = {"CoreGeneratorDefinitionRun": {"ok": ok, "task": {"id": "task-1"} if ok else None}}
    polls = [{"InfrahubTask": {"edges": [{"node": {"state": s}}]}} for s in task_states]
    return MagicMock(side_effect=[started, *polls])


def test_main_returns_zero_on_successful_run(capsys) -> None:
    """The run starts without waiting, its task is polled until COMPLETED, then main exits 0."""
    client = MagicMock()
    gen = MagicMock(id="gen-3")
    client.get = MagicMock(return_value=gen)
    client.execute_graphql = _graphql("PENDING", "RUNNING", "COMPLETED")

    with (
        patch.object(run_generator, "InfrahubClientSync", return_value=client),
        patch("scripts.run_generator.sys.argv", ["run_generator.py", "generate_l3vpn"]),
        patch("scripts.run_generator.time.sleep") as sleep,
    ):
        rc = run_generator.main()

    assert rc == 0
    assert "Generator 'generate_l3vpn' run completed" in capsys.readouterr().out
    calls = client.execute_graphql.call_args_list
    # The mutation must pass the generator's id, not its name, and must not hold the request open.
    assert calls[0].kwargs["variables"] == {"id": "gen-3"}
    assert "wait_until_completion: false" in calls[0].args[0]
    assert calls[1].kwargs["variables"] == {"ids": ["task-1"]}
    assert len(calls) == 4
    assert sleep.call_count == 2


def test_main_returns_nonzero_when_run_not_ok(capsys) -> None:
    """A mutation that returns ok=False → exit non-zero with the raw response logged."""
    client = MagicMock()
    client.get = MagicMock(return_value=MagicMock(id="gen-4"))
    client.execute_graphql = _graphql(ok=False)

    with (
        patch.object(run_generator, "InfrahubClientSync", return_value=client),
        patch("scripts.run_generator.sys.argv", ["run_generator.py", "generate_sdwan"]),
    ):
        rc = run_generator.main()

    assert rc == 1
    assert "did not report ok" in capsys.readouterr().err


def test_main_returns_nonzero_when_the_task_fails(capsys) -> None:
    """A run whose task ends FAILED exits non-zero and says where the traceback is."""
    client = MagicMock()
    client.get = MagicMock(return_value=MagicMock(id="gen-5"))
    client.execute_graphql = _graphql("RUNNING", "FAILED")

    with (
        patch.object(run_generator, "InfrahubClientSync", return_value=client),
        patch("scripts.run_generator.sys.argv", ["run_generator.py", "generate_sdwan"]),
        patch("scripts.run_generator.time.sleep"),
    ):
        rc = run_generator.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "run ended FAILED" in err
    assert "task-worker" in err


def test_wait_for_task_gives_up_after_the_timeout() -> None:
    """A task that never ends returns its last state once the deadline passes."""
    client = MagicMock()
    client.execute_graphql = MagicMock(
        return_value={"InfrahubTask": {"edges": [{"node": {"state": "RUNNING"}}]}}
    )
    monotonic_values = iter([0.0, 1.0, 999_999.0])

    with (
        patch("scripts.run_generator.time.sleep"),
        patch(
            "scripts.run_generator.time.monotonic",
            side_effect=lambda: next(monotonic_values, 1_000_000.0),
        ),
    ):
        assert run_generator._wait_for_task(client, "task-1") == "RUNNING"
    assert client.execute_graphql.call_count == 2


_COMPLETED = {"InfrahubTask": {"edges": [{"node": {"state": "COMPLETED"}}]}}

# What `execute_graphql` raises when one status request fails on the public sandbox,
# which runs behind Cloudflare.
_TRANSIENT_ERRORS = [
    pytest.param(ServerNotReachableError(address="http://x"), id="connection-refused"),
    pytest.param(ServerNotResponsiveError(url="http://x", timeout=60), id="read-timeout"),
    pytest.param(
        JsonDecodeError(url="http://x", content="error code: 524"), id="502-524-html-body"
    ),
    pytest.param(GraphQLError(errors=[{"message": "boom"}]), id="graphql-error"),
    pytest.param(httpx.RemoteProtocolError("connection dropped"), id="dropped-connection"),
    pytest.param(RateLimitError(url="http://x", attempts=3), id="rate-limited"),
    pytest.param(KeyError("data"), id="5xx-json-body-without-data"),
]


@pytest.mark.parametrize("error", _TRANSIENT_ERRORS)
def test_wait_for_task_keeps_polling_after_a_failed_status_request(error: Exception) -> None:
    """One failed status request must not stop the wait while the run succeeds on the server."""
    client = MagicMock()
    client.execute_graphql = MagicMock(side_effect=[error, _COMPLETED])

    with patch("scripts.run_generator.time.sleep") as sleep:
        assert run_generator._wait_for_task(client, "task-1") == "COMPLETED"

    assert client.execute_graphql.call_count == 2
    sleep.assert_called_once()


def test_wait_for_task_stops_on_a_refused_login() -> None:
    """A refused token will not heal by waiting: the error reaches the caller at once."""
    client = MagicMock()
    client.execute_graphql = MagicMock(side_effect=AuthenticationError("invalid token"))

    with patch("scripts.run_generator.time.sleep"), pytest.raises(AuthenticationError):
        run_generator._wait_for_task(client, "task-1")

    client.execute_graphql.assert_called_once()


def test_wait_for_task_raises_when_the_state_stays_unreadable_until_the_deadline() -> None:
    """A state never read is not "PENDING": the caller is told, with the last error."""
    client = MagicMock()
    client.execute_graphql = MagicMock(side_effect=ServerNotReachableError(address="http://x"))
    monotonic_values = iter([0.0, 1.0, 999_999.0])

    with (
        patch("scripts.run_generator.time.sleep"),
        patch(
            "scripts.run_generator.time.monotonic",
            side_effect=lambda: next(monotonic_values, 1_000_000.0),
        ),
        pytest.raises(run_generator.TaskStateUnreadableError, match="ServerNotReachableError"),
    ):
        run_generator._wait_for_task(client, "task-1")
    assert client.execute_graphql.call_count == 2


def test_wait_for_task_prints_a_persistent_error_once(capsys) -> None:
    """The same error on every poll is printed once, not every 3 seconds."""
    client = MagicMock()
    error = ServerNotReachableError(address="http://x")
    client.execute_graphql = MagicMock(side_effect=[error, error, error, _COMPLETED])

    with patch("scripts.run_generator.time.sleep"):
        assert run_generator._wait_for_task(client, "task-1") == "COMPLETED"

    assert capsys.readouterr().err.count("Could not read the state") == 1


def test_main_does_not_blame_the_clone_when_the_state_is_unreadable(capsys) -> None:
    """The traceback hint names a different cause: it must not follow an unreadable state."""
    client = MagicMock()
    client.get = MagicMock(return_value=MagicMock(id="gen-6"))
    client.execute_graphql = _graphql()

    with (
        patch.object(run_generator, "InfrahubClientSync", return_value=client),
        patch("scripts.run_generator.sys.argv", ["run_generator.py", "generate_sdwan"]),
        patch.object(
            run_generator,
            "_wait_for_task",
            side_effect=run_generator.TaskStateUnreadableError("state not read"),
        ),
    ):
        rc = run_generator.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "state not read" in err
    assert "task-worker" not in err


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param((502, b"<html>error code: 502</html>", "text/html"), id="html-502"),
        pytest.param((502, b'{"message": "Bad gateway"}', "application/json"), id="json-502"),
    ],
)
def test_wait_for_task_survives_a_5xx_from_a_real_http_server(
    failure: tuple[int, bytes, str],
) -> None:
    """A 5xx answer, read by the real SDK, does not stop the wait."""
    completed = b'{"data": {"InfrahubTask": {"edges": [{"node": {"state": "COMPLETED"}}]}}}'
    answers = [failure, (200, completed, "application/json")]

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", 0)))
            status, body, content_type = answers.pop(0)
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        address = f"http://127.0.0.1:{server.server_address[1]}"
        client = InfrahubClientSync(config=Config(address=address, api_token="x"))
        with patch("scripts.run_generator.time.sleep"):
            assert run_generator._wait_for_task(client, "task-1") == "COMPLETED"
    finally:
        server.shutdown()
        server.server_close()
    assert answers == []
