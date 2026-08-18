"""Push a config file to a running clab cEOS node via eAPI (JSON-RPC).

We deliberately avoid netmiko / SSH here. cEOS under containerlab is
slow on first interaction and netmiko's ``config_mode`` hard-codes a
10s read timeout that can't be tuned from outside; eAPI's
``runCmds`` method is a single HTTP POST and far more reliable.

The Arista template emits ``management api http-commands / protocol
http`` for the demo (HTTPS uses cEOS-lab's auto-generated self-signed
cert which can't be negotiated against modern Python TLS). This
script waits for port 80, then POSTs the config block inside a
``configure`` session and saves it.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

import requests

EAPI_PORT = 80
WAIT_TIMEOUT_SECONDS = 180
WAIT_POLL_INTERVAL_SECONDS = 3
HTTP_TIMEOUT_SECONDS = 120

# cEOS accepts TCP, and answers eAPI, before its agents have finished
# registering their CLI commands. Push during that window and a plain global
# command like `ip routing` is rejected with "Unavailable command (not
# supported on this hardware platform)" — the parser genuinely doesn't know
# the keyword yet. A fixed sleep can't cover it: how long the agents take
# scales with how many cEOS nodes the host is booting at once (the financial
# lab boots twelve). So probe for the capability instead of guessing.
READY_TIMEOUT_SECONDS = 300
READY_POLL_INTERVAL_SECONDS = 5
# A read-only command served by the same routing agent that owns
# `ip routing`. Once this parses, the config push will too.
READY_PROBE_CMDS = ("enable", "show ip route summary")


def _wait_for_port(host: str, port: int) -> None:
    """Block until ``host:port`` accepts a TCP connection.

    Raises:
        TimeoutError: If the port never opens within ``WAIT_TIMEOUT_SECONDS``.
    """
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(WAIT_POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"{host}:{port} never accepted TCP within {WAIT_TIMEOUT_SECONDS}s "
        f"(last error: {last_err!r})"
    )


def _post(host: str, cmds: list[str], timeout: int = HTTP_TIMEOUT_SECONDS) -> dict:
    """POST ``cmds`` to a node's eAPI and return the decoded JSON-RPC body.

    Args:
        host: Hostname of the cEOS node.
        cmds: Commands to run, in order.
        timeout: HTTP timeout in seconds.

    Returns:
        The parsed response body — a dict carrying either ``result`` or
        ``error``.
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "runCmds",
        # eAPI auto-handles mode transitions; no explicit `end` needed.
        "params": {"version": 1, "cmds": cmds, "format": "json"},
        "id": "push_arista",
    }
    resp = requests.post(
        f"http://{host}:{EAPI_PORT}/command-api",
        auth=("admin", "admin"),
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    body: dict = resp.json()
    return body


def _error_message(error: object) -> str:
    """Return a human-readable message from a JSON-RPC ``error`` member.

    JSON-RPC mandates an object with ``code``/``message``, and Arista eAPI
    conforms — but this runs against whatever answers on port 80 of a booting
    container, so a string or list body must not raise ``AttributeError`` from
    the caller's ``else:`` clause, which sits outside its ``try``.

    Args:
        error: The ``error`` member of a decoded JSON-RPC body.

    Returns:
        The ``message`` field when present, else the error rendered as text.
    """
    if isinstance(error, dict):
        return str(error.get("message", error))
    return str(error)


def _wait_for_eapi_ready(host: str) -> None:
    """Block until the node's routing agent answers a read-only probe.

    TCP being open is not enough — see :data:`READY_PROBE_CMDS`. Any error,
    HTTP or JSON-RPC, is treated as "not ready yet" and retried.

    Raises:
        TimeoutError: If the probe never succeeds within
            :data:`READY_TIMEOUT_SECONDS`.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last: str = "no attempt made"
    while time.monotonic() < deadline:
        try:
            body = _post(host, list(READY_PROBE_CMDS), timeout=30)
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
            last = repr(exc)
        else:
            if "error" not in body:
                return
            last = _error_message(body["error"])
        time.sleep(READY_POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"{host} eAPI never became ready within {READY_TIMEOUT_SECONDS}s "
        f"(last: {last}). The node is probably still booting its agents — "
        f"check `docker logs {host}`."
    )


def _strip_comments_and_blanks(text: str) -> list[str]:
    """Drop bang comments, empty lines, and CLI session markers.

    cEOS's eAPI ``runCmds`` rejects ``!`` comments and blank lines because
    they aren't real commands. The CLI accepts them as no-ops; eAPI is
    stricter.

    Also drops standalone ``end`` / ``exit`` lines — they're terminal-session
    markers that the template emits for human readability, but eAPI manages
    mode transitions itself and rejects them with ``Invalid input (at token
    0: 'end')``.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip().lower()
        if not stripped or stripped.startswith("!") or stripped in {"end", "exit"}:
            continue
        lines.append(raw)
    return lines


def main(config_path: str, host: str) -> int:
    """Push ``config_path`` to the cEOS device reachable at ``host`` via eAPI.

    Args:
        config_path: Path to the rendered configuration file.
        host: Hostname for the running clab container. containerlab
            registers each node as ``clab-<lab-name>-<node-name>`` in
            its embedded DNS.

    Returns:
        Exit code (0 on success).
    """
    text = Path(config_path).read_text(encoding="utf-8")
    commands = _strip_comments_and_blanks(text)
    print(f"Waiting for eAPI on {host}:{EAPI_PORT} (up to {WAIT_TIMEOUT_SECONDS}s)…")
    _wait_for_port(host, EAPI_PORT)
    print(f"Port open; waiting for agents to register their CLI (up to {READY_TIMEOUT_SECONDS}s)…")
    _wait_for_eapi_ready(host)

    print(f"POST http://{host}:{EAPI_PORT}/command-api  ({len(commands)} cmds)…")
    body = _post(host, ["enable", "configure", *commands, "write memory"])
    if "error" in body:
        err = body["error"]
        message = _error_message(err)
        # data carries per-command results; surface the failing one. The guard is
        # on `data` rather than `err`, because whatever answers on port 80 need
        # not be a well-formed eAPI: a dict `{"reason": ...}` made `[-1]` raise
        # KeyError and a string returned its last character, so the failure
        # report became a traceback and the per-node summary in tasks.py lost the
        # command that actually failed. _error_message below was hardened for the
        # same reason.
        data = err.get("data") if isinstance(err, dict) else None
        bad_index = data[-1] if isinstance(data, list) and data else data or {}
        print(
            f"eAPI error: {message}\nlast result: {bad_index}",
            file=sys.stderr,
        )
        return 1

    print(f"Pushed {len(commands)} commands to {host} via eAPI.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_path", help="Path to the rendered config file")
    parser.add_argument("host", help="SSH/eAPI hostname of the clab node")
    args = parser.parse_args()
    sys.exit(main(args.config_path, args.host))
