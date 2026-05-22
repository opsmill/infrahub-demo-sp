"""Push a config file to a running clab cEOS node over netmiko."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from netmiko import ConnectHandler


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
