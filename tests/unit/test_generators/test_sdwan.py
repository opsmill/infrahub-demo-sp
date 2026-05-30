"""Unit tests for the SD-WAN generator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.generate_sdwan import SdwanGenerator


def _make_gen(
    payload: dict, *, existing_members: list | None = None
) -> tuple[SdwanGenerator, MagicMock]:
    """Build a SdwanGenerator instance with a mocked client primed by ``payload``.

    ``existing_members`` controls what the edge group reports as already in it —
    use this to exercise the "add edge to group" path vs the idempotent no-op.
    """
    client = MagicMock()
    # The generator does `client.get(...)` for the CoreStandardGroup, then for
    # the ServiceSdwanSite, then for an existing DcimDevice if the site has
    # one, and finally for the ServiceSdwan row to flip status. We hand out
    # MagicMocks per-call so tests can inspect specific interactions.
    client.get = AsyncMock(
        side_effect=lambda **_: MagicMock(
            members=MagicMock(fetch=AsyncMock(), peers=existing_members or [], add=MagicMock()),
            save=AsyncMock(),
            id="mock-id",
            status=MagicMock(value="draft"),
            sdwan_edge=None,
            lan_address=None,
        )
    )
    client.create = AsyncMock()
    client.filters = AsyncMock(return_value=[])

    gen = SdwanGenerator.__new__(SdwanGenerator)
    gen.client = client
    gen.data = payload
    gen.branch = "main"
    return gen, client


def _svc_payload(
    name: str = "treasury-ops",
    vendor: str = "viptela",
    sites: list[dict] | None = None,
) -> dict:
    """Minimal ServiceSdwan + linked sites payload, shaped like the GraphQL result."""
    return {
        "ServiceSdwan": {
            "edges": [
                {
                    "node": {
                        "id": "svc-1",
                        "name": {"value": name},
                        "vendor": {"value": vendor},
                        "sites": {"edges": [{"node": s} for s in (sites or [])]},
                    }
                }
            ]
        },
    }


def _site(
    *,
    name: str = "london",
    location_short: str = "lon",
    lan_subnet: str = "10.10.0.0/24",
    has_edge: bool = False,
    has_lan: bool = False,
) -> dict:
    """One ServiceSdwanSite as it appears in the SDK's nested edge payload."""
    return {
        "id": f"site-{name}",
        "name": {"value": name},
        "location": {"node": {"shortname": {"value": location_short}}},
        "lan_subnet": {"node": {"prefix": {"value": lan_subnet}}},
        "sdwan_edge": {"node": {"id": f"edge-{name}"}} if has_edge else None,
        "lan_address": {"node": {"id": f"ip-{name}"}} if has_lan else None,
    }


@pytest.mark.asyncio
async def test_generator_skips_when_no_service_matched() -> None:
    """An empty payload is logged + returned — never raises."""
    gen, client = _make_gen({"ServiceSdwan": {"edges": []}})
    await gen.generate()
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_vendor_raises() -> None:
    """Vendors outside the table must error — there's no platform mapping to fall back on."""
    gen, _ = _make_gen(_svc_payload(vendor="meraki", sites=[_site()]))
    with pytest.raises(RuntimeError, match="Unknown SD-WAN vendor 'meraki'"):
        await gen.generate()


@pytest.mark.asyncio
async def test_viptela_uses_cisco_viptela_platform_and_edge_group() -> None:
    """Vendor 'viptela' → (cisco_viptela, cEdge-1000, sdwan_viptela_edges)."""
    payload = _svc_payload(vendor="viptela", sites=[_site(has_edge=False, has_lan=True)])
    gen, client = _make_gen(payload)
    with patch("generators.generate_sdwan.find_or_create_device", new=AsyncMock()) as foc:
        foc.return_value = MagicMock(id="edge-new")
        await gen.generate()
        foc.assert_awaited_once()
        kwargs = foc.await_args.kwargs
        assert kwargs["platform_name"] == "cisco_viptela"
        assert kwargs["device_type_name"] == "cEdge-1000"
        assert kwargs["role"] == "cpe"
        assert kwargs["location_hfid"] == "lon"

    # CoreStandardGroup lookup uses the viptela edge group name.
    group_call = next(
        c for c in client.get.await_args_list if c.kwargs.get("kind") == "CoreStandardGroup"
    )
    assert group_call.kwargs["name__value"] == "sdwan_viptela_edges"


@pytest.mark.asyncio
async def test_versa_uses_versa_flexvnf_platform_and_edge_group() -> None:
    """Vendor 'versa' → (versa_flexvnf, FlexVNF-200, sdwan_versa_edges)."""
    payload = _svc_payload(vendor="versa", sites=[_site(has_edge=False, has_lan=True)])
    gen, client = _make_gen(payload)
    with patch("generators.generate_sdwan.find_or_create_device", new=AsyncMock()) as foc:
        foc.return_value = MagicMock(id="edge-new")
        await gen.generate()
        kwargs = foc.await_args.kwargs
        assert kwargs["platform_name"] == "versa_flexvnf"
        assert kwargs["device_type_name"] == "FlexVNF-200"

    group_call = next(
        c for c in client.get.await_args_list if c.kwargs.get("kind") == "CoreStandardGroup"
    )
    assert group_call.kwargs["name__value"] == "sdwan_versa_edges"


@pytest.mark.asyncio
async def test_lan_address_first_usable_in_subnet() -> None:
    """The materialised LAN IP is network_address + 1 with the subnet's prefix length."""
    payload = _svc_payload(sites=[_site(lan_subnet="10.42.7.0/24")])
    gen, client = _make_gen(payload)
    with patch("generators.generate_sdwan.find_or_create_device", new=AsyncMock()):
        await gen.generate()

    ip_calls = [c for c in client.create.await_args_list if c.kwargs.get("kind") == "IpamIPAddress"]
    assert ip_calls, "Expected an IpamIPAddress create"
    assert ip_calls[0].kwargs["address"] == "10.42.7.1/24"


@pytest.mark.asyncio
async def test_existing_lan_address_not_recreated() -> None:
    """Sites that already have a lan_address peer are skipped — no second IPAddress create."""
    payload = _svc_payload(sites=[_site(has_lan=True)])
    gen, client = _make_gen(payload)
    with patch("generators.generate_sdwan.find_or_create_device", new=AsyncMock()):
        await gen.generate()
    assert not any(
        c for c in client.create.await_args_list if c.kwargs.get("kind") == "IpamIPAddress"
    )


@pytest.mark.asyncio
async def test_existing_edge_not_recreated() -> None:
    """Sites with `sdwan_edge` set fetch the device by id — no find_or_create_device call."""
    payload = _svc_payload(sites=[_site(has_edge=True, has_lan=True)])
    gen, _client = _make_gen(payload)
    with patch("generators.generate_sdwan.find_or_create_device", new=AsyncMock()) as foc:
        await gen.generate()
        foc.assert_not_called()
