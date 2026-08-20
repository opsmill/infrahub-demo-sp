"""Workarounds that must run before collection, for every suite.

The Infrahub deployment fixtures live in ``tests/integration/conftest.py``.
"""

from __future__ import annotations

import os
from typing import Any

import psutil

# The ``infrahub_testcontainers`` pytest plugin calls ``psutil.cpu_freq()`` unguarded at
# session start; on Apple Silicon it raises (``SystemError`` or ``RuntimeError``) and kills
# collection for every suite. Remove once fixed upstream.
_original_cpu_freq = psutil.cpu_freq


def _cpu_freq_or_none(*args: object, **kwargs: object) -> Any:  # noqa: ANN401 - mirrors psutil
    """Report CPU frequency, or ``None`` where the platform cannot."""
    try:
        return _original_cpu_freq(*args, **kwargs)
    except Exception:  # noqa: BLE001 - must degrade to None, never kill the session
        return None


psutil.cpu_freq = _cpu_freq_or_none

# docker/compose#13899: `up --wait` fails on the zero-replica service the packaged compose
# file defaults to. Drop when Compose ships the fix.
os.environ.setdefault("INFRAHUB_TESTING_TASKMGR_BACKGROUND_SVC_REPLICAS", "1")
