"""Cisco SD-WAN (Viptela / cEdge) config transform."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class SdwanViptela(InfrahubTransform):
    """Render a single Cisco Viptela cEdge config."""

    query = "sdwan_edge"

    async def transform(self, data: dict[str, Any]) -> str:
        """Render the Viptela Jinja2 template against the SD-WAN edge query.

        Args:
            data: Result of the ``sdwan_edge`` GraphQL query for one device.

        Returns:
            Rendered Cisco IOS-XE SD-WAN configuration as plain text.
        """
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(disabled_extensions=("j2",), default_for_string=False),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("sdwan_viptela.j2")
        return template.render(data=data)
