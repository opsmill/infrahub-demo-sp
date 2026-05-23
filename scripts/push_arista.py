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
# cEOS accepts TCP a few seconds before eAPI is responsive. Let the
# startup-config finish loading before we POST commands.
POST_PORT_SETTLE_SECONDS = 15
HTTP_TIMEOUT_SECONDS = 120


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


def _strip_comments_and_blanks(text: str) -> list[str]:
    """Drop bang/hash comments, empty lines, and CLI session markers.

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
    print(f"Port open; letting cEOS settle for {POST_PORT_SETTLE_SECONDS}s…")
    time.sleep(POST_PORT_SETTLE_SECONDS)

    payload = {
        "jsonrpc": "2.0",
        "method": "runCmds",
        "params": {
            "version": 1,
            # eAPI auto-handles mode transitions; no explicit `end` needed.
            "cmds": ["enable", "configure", *commands, "write memory"],
            "format": "json",
        },
        "id": "push_arista",
    }
    print(f"POST http://{host}:{EAPI_PORT}/command-api  ({len(commands)} cmds)…")
    resp = requests.post(
        f"http://{host}:{EAPI_PORT}/command-api",
        auth=("admin", "admin"),
        json=payload,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        err = body["error"]
        message = err.get("message", "unknown")
        # data carries per-command results; surface the failing one.
        bad_index = err.get("data", [{}])[-1] if err.get("data") else {}
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
