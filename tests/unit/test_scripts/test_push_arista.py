"""Unit tests for the cEOS eAPI push helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "push_arista.py"
spec = importlib.util.spec_from_file_location("push_arista", SCRIPT)
assert spec and spec.loader
push_arista = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push_arista)


def test_strip_drops_bang_comments_and_blanks() -> None:
    """`!` comments and blank lines are not real eAPI commands."""
    result = push_arista._strip_comments_and_blanks(
        "! header comment\n\nhostname pe-lon-arista\n!\ninterface Ethernet1\n"
    )
    assert result == ["hostname pe-lon-arista", "interface Ethernet1"]


def test_strip_drops_end_and_exit_session_markers() -> None:
    """`end` and `exit` are CLI session markers — eAPI rejects them."""
    result = push_arista._strip_comments_and_blanks(
        "hostname pe-lon-arista\nend\ninterface Ethernet1\nexit\n"
    )
    assert "end" not in result
    assert "exit" not in result
    assert result == ["hostname pe-lon-arista", "interface Ethernet1"]


def test_strip_preserves_indented_commands() -> None:
    """Leading whitespace is significant in some EOS contexts; keep it."""
    result = push_arista._strip_comments_and_blanks(
        "router bgp 65000\n   neighbor 10.0.0.2 peer group RR-MESH\n"
    )
    assert result == [
        "router bgp 65000",
        "   neighbor 10.0.0.2 peer group RR-MESH",
    ]


class _Resp:
    """Minimal stand-in for a `requests` response."""

    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def test_ready_probe_retries_until_the_routing_agent_answers(monkeypatch) -> None:
    """cEOS answers eAPI before its agents register their CLI commands.

    Pushing in that window fails on a plain `ip routing` with "not supported
    on this hardware platform". The probe must keep polling until a
    routing-agent-backed read command parses.
    """
    responses = [
        {"error": {"message": "Unavailable command (not supported on this hardware platform)"}},
        {"error": {"message": "Unavailable command (not supported on this hardware platform)"}},
        {"result": [{}, {}]},
    ]
    calls: list[list[str]] = []

    def fake_post(url, auth, json, timeout):  # noqa: A002 - mirror requests' signature
        calls.append(json["params"]["cmds"])
        return _Resp(responses[len(calls) - 1])

    monkeypatch.setattr(push_arista.requests, "post", fake_post)
    monkeypatch.setattr(push_arista.time, "sleep", lambda _: None)

    push_arista._wait_for_eapi_ready("clab-mpls-backbone-1-pe-05")

    assert len(calls) == 3, "should have retried past both not-ready responses"
    assert calls[0] == list(push_arista.READY_PROBE_CMDS)


def test_ready_probe_treats_transport_errors_as_not_ready(monkeypatch) -> None:
    """A refused connection mid-boot is a retry, not a crash."""
    state = {"n": 0}

    def fake_post(url, auth, json, timeout):  # noqa: A002
        state["n"] += 1
        if state["n"] < 3:
            raise OSError("connection reset by peer")
        return _Resp({"result": [{}, {}]})

    monkeypatch.setattr(push_arista.requests, "post", fake_post)
    monkeypatch.setattr(push_arista.time, "sleep", lambda _: None)

    push_arista._wait_for_eapi_ready("clab-mpls-backbone-1-pe-05")
    assert state["n"] == 3


def test_ready_probe_times_out_with_the_last_error(monkeypatch) -> None:
    """Give up eventually, and say what the node was still complaining about."""
    monkeypatch.setattr(
        push_arista.requests,
        "post",
        lambda url, auth, json, timeout: _Resp({"error": {"message": "agent not ready"}}),
    )
    monkeypatch.setattr(push_arista.time, "sleep", lambda _: None)
    monkeypatch.setattr(push_arista, "READY_TIMEOUT_SECONDS", 0.01)

    try:
        push_arista._wait_for_eapi_ready("clab-mpls-backbone-1-pe-05")
    except TimeoutError as exc:
        assert "agent not ready" in str(exc)
    else:
        raise AssertionError("expected TimeoutError")


def test_push_does_not_sleep_a_fixed_settle_window(monkeypatch) -> None:
    """The blind settle sleep is gone — readiness is probed, not guessed.

    A fixed sleep cannot scale with how many cEOS nodes the host boots at
    once, which is what made this flaky as the lab grew to twelve.
    """
    assert not hasattr(push_arista, "POST_PORT_SETTLE_SECONDS")
