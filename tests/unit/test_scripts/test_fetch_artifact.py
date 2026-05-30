"""Unit tests for `scripts/fetch_artifact.py`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts import fetch_artifact


def _mock_artifact(storage_id: str | None) -> MagicMock:
    """Mock a CoreArtifact row with .storage_id.value."""
    art = MagicMock()
    art.storage_id.value = storage_id
    return art


def _fake_response(body: bytes) -> MagicMock:
    """Context-manager response for urllib.urlopen."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=body)
    return resp


def test_writes_artifact_body_to_stdout_buffer(capsysbinary) -> None:
    """Happy path: storage_id present → GET storage endpoint → write bytes to stdout."""
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.get = MagicMock(return_value=_mock_artifact("store-xyz"))
    body = b"hostname pe-arista\n"

    with (
        patch.object(fetch_artifact, "InfrahubClientSync", return_value=client),
        patch.dict("os.environ", {"INFRAHUB_API_TOKEN": "tok"}, clear=False),
        patch("scripts.fetch_artifact.sys.argv", ["fetch_artifact.py", "pe-arista-eos"]),
        patch(
            "scripts.fetch_artifact.urllib.request.urlopen",
            return_value=_fake_response(body),
        ) as urlopen,
    ):
        rc = fetch_artifact.main()

    assert rc == 0
    captured = capsysbinary.readouterr()
    assert captured.out == body
    # Verify the URL and auth header are right.
    request = urlopen.call_args.args[0]
    assert request.full_url == "http://localhost:8000/api/storage/object/store-xyz"
    assert request.headers["X-infrahub-key"] == "tok"


def test_missing_storage_id_exits_nonzero(capsys) -> None:
    """An artifact that hasn't rendered yet (no storage_id) should error, not crash."""
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.get = MagicMock(return_value=_mock_artifact(None))

    with (
        patch.object(fetch_artifact, "InfrahubClientSync", return_value=client),
        patch("scripts.fetch_artifact.sys.argv", ["fetch_artifact.py", "pe-arista-eos"]),
    ):
        rc = fetch_artifact.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "pe-arista-eos" in err
    assert "no storage_id" in err


def test_lookup_uses_name_filter() -> None:
    """The lookup must filter by `name__value=…`, not by id — a regression bug
    that shipped in PR #64 used `id__value` and returned the wrong artifact."""
    client = MagicMock()
    client.address = "http://localhost:8000"
    client.get = MagicMock(return_value=_mock_artifact("store-1"))

    with (
        patch.object(fetch_artifact, "InfrahubClientSync", return_value=client),
        patch.dict("os.environ", {"INFRAHUB_API_TOKEN": "tok"}, clear=False),
        patch("scripts.fetch_artifact.sys.argv", ["fetch_artifact.py", "clab-mpls-topology"]),
        patch("scripts.fetch_artifact.urllib.request.urlopen", return_value=_fake_response(b"")),
    ):
        fetch_artifact.main()

    client.get.assert_called_once_with(kind="CoreArtifact", name__value="clab-mpls-topology")
