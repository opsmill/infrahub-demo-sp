"""Fixtures and workarounds that apply to the whole test session.

The Infrahub deployment lives in ``tests/integration/conftest.py``. This module
holds only what must run before collection, so the unit suite is affected by it
too -- it loads whenever ``infrahub-testcontainers`` is installed, regardless of
which suite is being run.
"""

from __future__ import annotations

import os
from typing import Any

import psutil

# ``infrahub_testcontainers`` registers a pytest plugin -- entry point
# ``pytest-infrahub-performance-test`` -- whose ``pytest_sessionstart`` builds a
# host profile: ``plugin.py:94`` -> ``performance_test.py:44 get_system_stats()``
# -> ``host.py:15 psutil.cpu_freq()``, called unguarded. On Apple Silicon that
# raises, killing the whole session with INTERNALERROR before collection -- unit
# tests included, because the plugin loads whenever the package is installed.
# ``host.py:19-21`` already read the result as ``cpu_freq.current if cpu_freq
# else None``, so upstream meant it to be nullable and missed that the call
# itself can raise. Remove this once that is fixed upstream.
#
# Catching ``Exception`` is deliberate and not laziness. Two different failures
# have been observed on the same platform family: ``SystemError: <built-in
# function cpu_freq> returned a result with an exception set``, and
# ``RuntimeError: 'voltage-states1-sram' property not found``. ``SystemError``
# derives from ``Exception`` directly, so a narrower clause listing
# ``RuntimeError``, ``OSError`` and ``AttributeError`` does not catch it. All
# three frequency fields are cosmetic telemetry; no reading here is worth an
# INTERNALERROR.
_original_cpu_freq = psutil.cpu_freq


def _cpu_freq_or_none(*args: object, **kwargs: object) -> Any:  # noqa: ANN401 - mirrors psutil
    """Report CPU frequency, or ``None`` where the platform cannot.

    Args:
        *args: Passed through to ``psutil.cpu_freq``.
        **kwargs: Passed through to ``psutil.cpu_freq``.

    Returns:
        Whatever ``psutil.cpu_freq`` returns, or ``None`` when it raises.
    """
    try:
        return _original_cpu_freq(*args, **kwargs)
    except Exception:  # noqa: BLE001 - must degrade to None, never kill the session
        return None


psutil.cpu_freq = _cpu_freq_or_none

# docker/compose#13899: `up --wait` fails on a project containing a zero-replica
# service, reporting it as a missing dependency. The packaged compose file
# declares `task-manager-background-svc` with
# `replicas: ${INFRAHUB_TESTING_TASKMGR_BACKGROUND_SVC_REPLICAS:-0}`
# (container.py:124), so the default trips it. Scheduling one replica is
# harmless -- nothing depends on that service. `setdefault` so an explicit value
# still wins. Drop this when Compose ships the fix.
os.environ.setdefault("INFRAHUB_TESTING_TASKMGR_BACKGROUND_SVC_REPLICAS", "1")
