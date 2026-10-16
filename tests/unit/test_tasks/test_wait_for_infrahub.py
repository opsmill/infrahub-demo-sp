"""Readiness wait that ``invoke bootstrap`` runs before loading schemas.

``docker compose up -d`` returns when containers have started, not when the
Infrahub API is listening, so ``invoke start bootstrap`` used to fail at once
with "Unable to connect to 'http://localhost:8000'".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from invoke.exceptions import Exit

from tasks import _infrahub_is_ready, _wait_for_infrahub


def test_an_unreachable_server_is_not_ready() -> None:
    """A refused connection counts as "not ready yet", not as an error."""
    with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError):
        assert _infrahub_is_ready("http://localhost:8000") is False


def test_the_wait_returns_once_the_api_answers() -> None:
    """The wait polls until the API answers, then returns."""
    with (
        patch("tasks._infrahub_is_ready", side_effect=[False, False, True]) as ready,
        patch("tasks.time.sleep"),
    ):
        _wait_for_infrahub(max_retries=10, sleep_time=0)
    assert ready.call_count == 3


def test_the_wait_exits_when_the_api_never_answers() -> None:
    """A server that never comes up stops bootstrap after the last attempt."""
    with (
        patch("tasks._infrahub_is_ready", return_value=False) as ready,
        patch("tasks.time.sleep"),
        pytest.raises(Exit),
    ):
        _wait_for_infrahub(max_retries=5, sleep_time=0)
    assert ready.call_count == 5
