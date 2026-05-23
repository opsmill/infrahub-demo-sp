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
