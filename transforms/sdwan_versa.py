"""Versa Networks VOS (FlexVNF) SD-WAN config transform."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class SdwanVersa(InfrahubTransform):
    """Render a single Versa Networks FlexVNF SD-WAN config."""

    query = "sdwan_edge"

    async def transform(self, data: dict[str, Any]) -> str:
        """Render the Versa Jinja2 template against the SD-WAN edge query.

        Args:
            data: Result of the ``sdwan_edge`` GraphQL query for one device.

        Returns:
            Rendered Versa VOS CLI configuration as plain text.
        """
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(disabled_extensions=("j2",), default_for_string=False),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("sdwan_versa.j2")
        return template.render(data=data)
