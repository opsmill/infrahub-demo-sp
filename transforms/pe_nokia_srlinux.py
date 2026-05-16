"""Nokia SR Linux PE config transform.

Renders the same logical PE config as ``pe_nokia_sros`` but emits SR Linux
``set /`` CLI syntax — needed when running the lab on the publicly-available
SR Linux image (see ``DcimPlatform.containerlab_os = srl``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class PeNokiaSrLinux(InfrahubTransform):
    """Render Nokia SR Linux PE configuration."""

    query = "pe"

    async def transform(self, data: dict[str, Any]) -> str:
        """Render the SR Linux Jinja2 template against query data.

        Args:
            data: Result of the ``pe`` GraphQL query for this device.

        Returns:
            Rendered SR Linux configuration as plain text.
        """
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(disabled_extensions=("j2",), default_for_string=False),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("pe_nokia_srlinux.j2")
        return template.render(data=data)
