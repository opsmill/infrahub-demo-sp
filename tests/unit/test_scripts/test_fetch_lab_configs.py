"""Unit tests for `scripts/fetch_lab_configs.py`."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from scripts import fetch_lab_configs


def _device(name: str, containerlab_os: str | None) -> MagicMock:
    """Mock a DcimDevice with the .platform.peer.containerlab_os.value shape."""
    device = MagicMock()
    device.id = f"{name}-id"
    device.name.value = name
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
    """Return a client mock whose `filters` dispatches on the requested kind.

    The script queries devices once per role in ``LAB_ROLES`` and artifacts once
    per device, so a positional `side_effect` list would break whenever the role
    set changes. Dispatching on the call's kwargs keeps the tests stable.
    """
    artifacts_by_device_id = artifacts_by_device_id or {}
    client = MagicMock()
    client.address = "http://localhost:8000"

    def _filters(**kwargs: Any) -> list[MagicMock]:
        if kwargs.get("kind") == "DcimDevice":
            return devices_by_role.get(kwargs["role__value"], [])
        return artifacts_by_device_id.get(kwargs["object__ids"][0], [])

    client.filters = MagicMock(side_effect=_filters)
    client.get = MagicMock(side_effect=lambda **kw: MagicMock(id=f"defn-{kw['name__value']}"))
    return client


def _fake_response(body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=body)
    return resp


def test_writes_one_file_per_labbed_device(tmp_path) -> None:
    """Every (role, containerlab_os) pair in the table gets a config file.

    Covers both roles the lab boots: the backbone PEs and the pre-provisioned
    CE routers.
    """
    pe_arista = _device("pe-lon-arista", "ceos")
    pe_nokia = _device("pe-par-nokia", "srl")
    ce = _device("ce-trading-lon", "ceos")
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


def test_ce_uses_the_ce_artifact_definition(tmp_path) -> None:
    """A cEOS CE resolves ce-arista-eos-config, not the PE definition.

    Role and clab kind together pick the definition — a CE and a PE are both
    `ceos`, so keying on the kind alone would render PE config onto the CE.
    """
    ce = _device("ce-ib-zrh", "ceos")
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


def test_device_with_unsupported_kind_is_skipped(tmp_path) -> None:
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


def test_device_with_no_platform_is_skipped(tmp_path) -> None:
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


def test_artifact_without_storage_id_logs_warning_continues(tmp_path, capsys) -> None:
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


def test_missing_artifact_logs_warning(tmp_path, capsys) -> None:
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
