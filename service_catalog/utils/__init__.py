"""Helpers for the Streamlit service catalog."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import streamlit as st
from infrahub_sdk.client import InfrahubClient
from infrahub_sdk.config import Config

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


def client_for(branch: str = "main") -> InfrahubClient:
    """Return an InfrahubClient bound to ``branch``.

    Reads ``INFRAHUB_ADDRESS`` and ``INFRAHUB_API_TOKEN`` from the environment.

    Args:
        branch: Infrahub branch name to bind the client to.

    Returns:
        A configured InfrahubClient instance.
    """
    config = Config(
        address=os.environ["INFRAHUB_ADDRESS"],
        api_token=os.environ["INFRAHUB_API_TOKEN"],
        default_branch=branch,
    )
    return InfrahubClient(config=config)


def display_logo() -> None:
    """Render the Infrahub logo above the sidebar navigation.

    Uses ``st.logo()`` to place the logo above the page navigation links.
    Streamlit automatically swaps between the light and dark variants
    based on the active theme.
    """
    logo_light = ASSETS_DIR / "infrahub-hori.svg"
    logo_dark = ASSETS_DIR / "infrahub-hori-dark.svg"
    if logo_light.exists() and logo_dark.exists():
        st.logo(str(logo_light), icon_image=str(logo_dark))
    elif logo_light.exists():
        st.logo(str(logo_light))
    else:
        st.sidebar.markdown("### Infrahub Service Catalog")


def run_async(coro: Any) -> Any:
    """Run an async function from synchronous Streamlit code.

    Args:
        coro: Awaitable coroutine to execute.

    Returns:
        The result of the coroutine.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def wait_for_pipeline(
    client: InfrahubClient,
    proposed_change_id: str,
    *,
    timeout: int | None = None,
) -> str:
    """Poll the proposed-change pipeline until it completes or times out.

    Args:
        client: Infrahub SDK client.
        proposed_change_id: ID of the CoreProposedChange row.
        timeout: Seconds before giving up. Defaults to ``GENERATOR_WAIT_TIME`` env.

    Returns:
        Final state string (e.g. ``"completed"``, ``"failed"``, ``"timed_out"``).
    """
    deadline = time.time() + (timeout or int(os.environ.get("GENERATOR_WAIT_TIME", "60")))
    while time.time() < deadline:
        pc = run_async(client.get(kind="CoreProposedChange", id=proposed_change_id))
        state = pc.state.value if hasattr(pc, "state") else "unknown"
        if state in ("completed", "failed", "merged", "cancelled"):
            return state
        time.sleep(2)
    return "timed_out"
