"""Unit tests for `scripts/fetch_lab_configs.py`."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts import fetch_lab_configs


def _device(name: str, containerlab_os: str | None, role: str | None = "pe") -> MagicMock:
    """Mock a DcimDevice with the .platform.peer.containerlab_os.value shape.

    Args:
        name: Device name.
        containerlab_os: The platform's lab image kind, or ``None`` for a device
            with no platform at all.
        role: The device role, or ``None`` to model the optional field unset.
    """
    device = MagicMock()
    device.id = f"{name}-id"
    device.name.value = name
    if role is None:
        device.role = None
    else:
        device.role.value = role
    if containerlab_os is None:
        device.platform = None
    else:
        device.platform.peer.containerlab_os.value = containerlab_os
    return device


def _artifact(storage_id: str | None) -> MagicMock:
    a = MagicMock()
    a.storage_id.value = storage_id
    return a


def _client(
    devices_by_role: dict[str, list[MagicMock]],
    artifacts_by_device_id: dict[str, list[MagicMock]] | None = None,
) -> MagicMock:
    """Return a client mock serving devices from `all` and artifacts from `filters`.

    The script enumerates every device once — selection is by platform, not by
    role, so that it cannot disagree with the topology transform — and then
    queries artifacts per device. `devices_by_role` still keys the fixture by
    role because that is how the tests read, but every device is returned from
    the single `all` call.
    """
    artifacts_by_device_id = artifacts_by_device_id or {}
    client = MagicMock()
    client.address = "http://localhost:8000"
    every_device = [d for devices in devices_by_role.values() for d in devices]

    def _filters(**kwargs: Any) -> list[MagicMock]:
        return artifacts_by_device_id.get(kwargs["object__ids"][0], [])

    client.all = MagicMock(return_value=every_device)
    client.filters = MagicMock(side_effect=_filters)
    client.get = MagicMock(side_effect=lambda **kw: MagicMock(id=f"defn-{kw['name__value']}"))
    return client


def _fake_response(body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=body)
    return resp


def test_writes_one_file_per_labbed_device(tmp_path: Path) -> None:
    """Every (role, containerlab_os) pair in the table gets a config file.

    Covers both roles the lab boots: the backbone PEs and the pre-provisioned
    CE routers.
    """
    pe_arista = _device("pe-lon-arista", "ceos")
    pe_nokia = _device("pe-par-nokia", "srl")
    ce = _device("ce-trading-lon", "ceos", role="cpe")
    client = _client(
        devices_by_role={"pe": [pe_arista, pe_nokia], "cpe": [ce]},
        artifacts_by_device_id={
            pe_arista.id: [_artifact("store-arista")],
            pe_nokia.id: [_artifact("store-nokia")],
            ce.id: [_artifact("store-ce")],
        },
    )

    with (
        patch.object(fetch_lab_configs, "InfrahubClientSync", return_value=client),
        patch.dict("os.environ", {"INFRAHUB_API_TOKEN": "tok"}, clear=False),
        patch(
            "scripts.fetch_lab_configs.sys.argv",
            ["fetch_lab_configs.py", "--out-dir", str(tmp_path)],
        ),
        patch(
            "scripts.fetch_lab_configs.urllib.request.urlopen",
            side_effect=[
                _fake_response(b"arista-cfg"),
                _fake_response(b"nokia-cfg"),
                _fake_response(b"ce-cfg"),
            ],
        ),
    ):
        rc = fetch_lab_configs.main()

    assert rc == 0
    assert (tmp_path / "pe-lon-arista.cfg").read_bytes() == b"arista-cfg"
    assert (tmp_path / "pe-par-nokia.cfg").read_bytes() == b"nokia-cfg"
    assert (tmp_path / "ce-trading-lon.cfg").read_bytes() == b"ce-cfg"


def test_ce_uses_the_ce_artifact_definition(tmp_path: Path) -> None:
    """A cEOS CE resolves ce-arista-eos-config, not the PE definition.

    Role and clab kind together pick the definition — a CE and a PE are both
    `ceos`, so keying on the kind alone would render PE config onto the CE.
    """
    ce = _device("ce-ib-zrh", "ceos", role="cpe")
    client = _client(
        devices_by_role={"pe": [], "cpe": [ce]},
        artifacts_by_device_id={ce.id: [_artifact("store-ce")]},
    )

    with (
        patch.object(fetch_lab_configs, "InfrahubClientSync", return_value=client),
        patch.dict("os.environ", {"INFRAHUB_API_TOKEN": "tok"}, clear=False),
        patch(
            "scripts.fetch_lab_configs.sys.argv",
            ["fetch_lab_configs.py", "--out-dir", str(tmp_path)],
        ),
        patch(
            "scripts.fetch_lab_configs.urllib.request.urlopen",
            side_effect=[_fake_response(b"ce-cfg")],
        ),
    ):
        rc = fetch_lab_configs.main()

    assert rc == 0
    definition_names = [call.kwargs["name__value"] for call in client.get.call_args_list]
    assert definition_names == ["ce-arista-eos-config"]


def test_device_with_unsupported_kind_is_skipped(tmp_path: Path) -> None:
    """A device whose containerlab_os isn't in the table doesn't get fetched.

    DEFINITION_BY_ROLE_AND_KIND today only covers ceos/srl. Other kinds
    (cisco_iosxr, juniper_junos, nokia_sros — the production-only artifacts) are
    intentionally skipped because containerlab can't boot those images.
    """
    client = _client(devices_by_role={"pe": [_device("pe-fra-cisco", "iosxr")]})

    with (
        patch.object(fetch_lab_configs, "InfrahubClientSync", return_value=client),
        patch(
            "scripts.fetch_lab_configs.sys.argv",
            ["fetch_lab_configs.py", "--out-dir", str(tmp_path)],
        ),
    ):
        rc = fetch_lab_configs.main()

    assert rc == 1  # No labbed configs written → non-zero.
    assert list(tmp_path.iterdir()) == []
    # No artifact lookup should have happened — filtered out before the fetch.
    client.get.assert_not_called()


def test_device_with_no_platform_is_skipped(tmp_path: Path) -> None:
    """Devices without a platform peer must not crash the loop (defensive read)."""
    client = _client(devices_by_role={"pe": [_device("pe-orphan", None)]})

    with (
        patch.object(fetch_lab_configs, "InfrahubClientSync", return_value=client),
        patch(
            "scripts.fetch_lab_configs.sys.argv",
            ["fetch_lab_configs.py", "--out-dir", str(tmp_path)],
        ),
    ):
        rc = fetch_lab_configs.main()

    assert rc == 1
    assert list(tmp_path.iterdir()) == []


def test_artifact_without_storage_id_logs_warning_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing storage_id → warn on stderr, don't crash, return 1 if no others wrote."""
    pe = _device("pe-lon-arista", "ceos")
    client = _client(
        devices_by_role={"pe": [pe]},
        artifacts_by_device_id={pe.id: [_artifact(None)]},
    )

    with (
        patch.object(fetch_lab_configs, "InfrahubClientSync", return_value=client),
        patch.dict("os.environ", {"INFRAHUB_API_TOKEN": "tok"}, clear=False),
        patch(
            "scripts.fetch_lab_configs.sys.argv",
            ["fetch_lab_configs.py", "--out-dir", str(tmp_path)],
        ),
    ):
        rc = fetch_lab_configs.main()

    assert rc == 1
    assert "no storage_id" in capsys.readouterr().err


def test_missing_artifact_logs_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A device that has no matching CoreArtifact yet logs and skips."""
    pe = _device("pe-par-nokia", "srl")
    client = _client(devices_by_role={"pe": [pe]}, artifacts_by_device_id={pe.id: []})

    with (
        patch.object(fetch_lab_configs, "InfrahubClientSync", return_value=client),
        patch(
            "scripts.fetch_lab_configs.sys.argv",
            ["fetch_lab_configs.py", "--out-dir", str(tmp_path)],
        ),
    ):
        rc = fetch_lab_configs.main()

    assert rc == 1
    assert "no pe-nokia-srlinux-config artifact" in capsys.readouterr().err


def test_labbed_device_with_no_role_is_still_visible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Selection is by platform, matching the topology transform.

    `role` is optional in the schema and the transform's CE gate is role-blind,
    so a wired CE on a labbed platform with its role unset used to get a
    startup-config entry in the topology and no fetched file — and containerlab
    aborted on the missing mount. It must now be visible here: either fetched,
    or reported as unmapped. Never silently skipped.
    """
    orphan = _device("ce-unset-role", "ceos", role=None)
    client = _client(devices_by_role={"pe": [orphan]})

    with (
        patch.object(fetch_lab_configs, "InfrahubClientSync", return_value=client),
        patch.dict("os.environ", {"INFRAHUB_API_TOKEN": "tok"}, clear=False),
        patch(
            "scripts.fetch_lab_configs.sys.argv",
            ["fetch_lab_configs.py", "--out-dir", str(tmp_path)],
        ),
    ):
        rc = fetch_lab_configs.main()

    assert rc == 1
    assert "no artifact definition for role=None" in capsys.readouterr().err
