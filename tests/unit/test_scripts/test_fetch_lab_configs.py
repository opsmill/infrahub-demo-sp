"""Unit tests for `scripts/fetch_lab_configs.py`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts import fetch_lab_configs


def _pe(name: str, containerlab_os: str | None) -> MagicMock:
    """Mock a DcimDevice with the .platform.peer.containerlab_os.value shape."""
    pe = MagicMock()
    pe.id = f"pe-{name}-id"
    pe.name.value = name
    if containerlab_os is None:
        pe.platform = None
    else:
        pe.platform.peer.containerlab_os.value = containerlab_os
    return pe


def _artifact(storage_id: str | None) -> MagicMock:
    a = MagicMock()
    a.storage_id.value = storage_id
    return a


def _fake_response(body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=body)
    return resp


def test_writes_one_file_per_labbed_pe(tmp_path, capsys) -> None:
    """Each PE with a containerlab_os in KIND_TO_DEFINITION gets a config file."""
    pe_arista = _pe("pe-lon-arista", "ceos")
    pe_nokia = _pe("pe-par-nokia", "srl")
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.filters = MagicMock(
        side_effect=[
            [pe_arista, pe_nokia],  # DcimDevice filter
            [_artifact("store-arista")],  # CoreArtifact for arista
            [_artifact("store-nokia")],  # CoreArtifact for nokia
        ]
    )
    client.get = MagicMock(side_effect=[MagicMock(id="defn-arista"), MagicMock(id="defn-nokia")])

    with (
        patch.object(fetch_lab_configs, "InfrahubClientSync", return_value=client),
        patch.dict("os.environ", {"INFRAHUB_API_TOKEN": "tok"}, clear=False),
        patch(
            "scripts.fetch_lab_configs.sys.argv",
            ["fetch_lab_configs.py", "--out-dir", str(tmp_path)],
        ),
        patch(
            "scripts.fetch_lab_configs.urllib.request.urlopen",
            side_effect=[_fake_response(b"arista-cfg"), _fake_response(b"nokia-cfg")],
        ),
    ):
        rc = fetch_lab_configs.main()

    assert rc == 0
    assert (tmp_path / "pe-lon-arista.cfg").read_bytes() == b"arista-cfg"
    assert (tmp_path / "pe-par-nokia.cfg").read_bytes() == b"nokia-cfg"


def test_pe_with_unsupported_kind_is_skipped(tmp_path) -> None:
    """A PE whose platform.containerlab_os isn't in the table doesn't get fetched.

    KIND_TO_DEFINITION today only covers ceos/srl. Other kinds (cisco_iosxr,
    juniper_junos, nokia_sros — the production-only artifacts) are intentionally
    skipped because containerlab can't boot those images.
    """
    pe_skipped = _pe("pe-fra-cisco", "iosxr")  # not in the table
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.filters = MagicMock(return_value=[pe_skipped])

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
    # No urlopen call should have been made — the PE was filtered out before the fetch.
    client.get.assert_not_called()


def test_pe_with_no_platform_is_skipped(tmp_path) -> None:
    """Devices without a platform peer must not crash the loop (defensive read)."""
    pe_no_plat = _pe("pe-orphan", None)
    client = MagicMock()
    client.filters = MagicMock(return_value=[pe_no_plat])

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
    pe = _pe("pe-lon-arista", "ceos")
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.filters = MagicMock(side_effect=[[pe], [_artifact(None)]])
    client.get = MagicMock(return_value=MagicMock(id="defn-1"))

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
    """A PE that has no matching CoreArtifact yet logs and skips."""
    pe = _pe("pe-par-nokia", "srl")
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.filters = MagicMock(side_effect=[[pe], []])  # empty artifacts list
    client.get = MagicMock(return_value=MagicMock(id="defn-1"))

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
