"""Push a config file to a running clab cEOS node over netmiko."""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

from netmiko import ConnectHandler

SSH_PORT = 22
WAIT_TIMEOUT_SECONDS = 180
WAIT_POLL_INTERVAL_SECONDS = 3


def _wait_for_ssh(host: str) -> None:
    """Block until ``host:22`` accepts a TCP connection.

    cEOS takes 1-3 minutes after `containerlab deploy` returns before
    its SSHD is listening; netmiko's connect raises immediately on a
    refused connection, so poll the socket first.

    Raises:
        TimeoutError: If SSH never opens within ``WAIT_TIMEOUT_SECONDS``.
    """
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, SSH_PORT), timeout=2):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(WAIT_POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"{host}:{SSH_PORT} never accepted TCP within {WAIT_TIMEOUT_SECONDS}s "
        f"(last error: {last_err!r})"
    )


def main(config_path: str, host: str) -> int:
    """Push ``config_path`` to the cEOS device reachable at ``host``.

    Args:
        config_path: Path to the rendered configuration file.
        host: SSH hostname for the running clab container. containerlab
            registers each node as ``clab-<lab-name>-<node-name>`` in
            its embedded DNS, so the lab task is responsible for
            assembling the full hostname before invoking this script.

    Returns:
        Exit code (0 on success).
    """
    text = Path(config_path).read_text(encoding="utf-8")
    print(f"Waiting for SSH on {host}:{SSH_PORT} (up to {WAIT_TIMEOUT_SECONDS}s)…")
    _wait_for_ssh(host)
    conn = ConnectHandler(
        device_type="arista_eos",
        host=host,
        username="admin",
        password="admin",
    )
    conn.send_config_set(text.splitlines())
    conn.save_config()
    conn.disconnect()
    print(f"Pushed {len(text.splitlines())} lines to {host}.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_path", help="Path to the rendered config file")
    parser.add_argument("host", help="SSH hostname of the clab node")
    args = parser.parse_args()
    sys.exit(main(args.config_path, args.host))
