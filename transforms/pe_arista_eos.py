"""Arista EOS PE config transform."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class PeAristaEos(InfrahubTransform):
    """Render Arista EOS PE configuration."""

    query = "pe"

    async def transform(self, data: dict[str, Any]) -> str:
        """Render the EOS Jinja2 template against query data.

        Args:
            data: Result of the ``pe`` GraphQL query for this device.

        Returns:
            Rendered EOS configuration as plain text.
        """
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(disabled_extensions=("j2",), default_for_string=False),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )
        template = env.get_template("pe_arista_eos.j2")
        return template.render(data=data)
