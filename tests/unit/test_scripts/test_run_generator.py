"""Unit tests for `scripts/run_generator.py`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from infrahub_sdk.exceptions import NodeNotFoundError

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


def test_main_returns_zero_on_successful_run(capsys) -> None:
    """Successful mutation reports ok=True; main exits 0 with a completion line."""
    client = MagicMock()
    gen = MagicMock(id="gen-3")
    client.get = MagicMock(return_value=gen)
    client.execute_graphql = MagicMock(return_value={"CoreGeneratorDefinitionRun": {"ok": True}})

    with (
        patch.object(run_generator, "InfrahubClientSync", return_value=client),
        patch("scripts.run_generator.sys.argv", ["run_generator.py", "generate_l3vpn"]),
    ):
        rc = run_generator.main()

    assert rc == 0
    assert "Generator 'generate_l3vpn' run completed" in capsys.readouterr().out
    # The mutation must pass the generator's id, not its name.
    assert client.execute_graphql.call_args.kwargs["variables"] == {"id": "gen-3"}


def test_main_returns_nonzero_when_run_not_ok(capsys) -> None:
    """A mutation that returns ok=False → exit non-zero with the raw response logged."""
    client = MagicMock()
    client.get = MagicMock(return_value=MagicMock(id="gen-4"))
    client.execute_graphql = MagicMock(return_value={"CoreGeneratorDefinitionRun": {"ok": False}})

    with (
        patch.object(run_generator, "InfrahubClientSync", return_value=client),
        patch("scripts.run_generator.sys.argv", ["run_generator.py", "generate_sdwan"]),
    ):
        rc = run_generator.main()

    assert rc == 1
    assert "did not report ok" in capsys.readouterr().err
