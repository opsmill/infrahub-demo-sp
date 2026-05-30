"""Unit tests for the SD-WAN generator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.generate_sdwan import SdwanGenerator


def _make_gen(
    payload: dict,
    *,
    existing_members: list | None = None,
    existing_ip: list | None = None,
) -> tuple[SdwanGenerator, MagicMock]:
    """Build a SdwanGenerator with a mocked client primed by ``payload``.

    Idempotency is derived from deterministic keys via ``client.filters`` (the
    edge by name inside ``find_or_create_device``; the LAN IP by address) rather
    than the query payload. ``existing_ip`` controls what the IpamIPAddress
    lookup returns; ``existing_members`` what the edge group already contains.
    """
    client = MagicMock()
    group = MagicMock(
        members=MagicMock(fetch=AsyncMock(), peers=existing_members or [], add=MagicMock()),
        save=AsyncMock(),
        id="group-id",
    )

    def get_side_effect(**kwargs: object) -> MagicMock:
        kind = kwargs.get("kind")
        if kind == "CoreStandardGroup":
            return group
        if kind == "ServiceSdwanSite":
            return MagicMock(id="site-id", status=MagicMock(value="draft"), save=AsyncMock())
        if kind == "ServiceSdwan":
            return MagicMock(status=MagicMock(value="draft"), save=AsyncMock())
        return MagicMock(save=AsyncMock(), id="mock-id")

    client.get = AsyncMock(side_effect=get_side_effect)
    client.create = AsyncMock(return_value=MagicMock(id="created-id", save=AsyncMock()))
    client.filters = AsyncMock(return_value=existing_ip or [])

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
) -> dict:
    """One ServiceSdwanSite as it appears in the (non-self-referential) query payload."""
    return {
        "id": f"site-{name}",
        "name": {"value": name},
        "location": {"node": {"shortname": {"value": location_short}}},
        "lan_subnet": {"node": {"prefix": {"value": lan_subnet}}},
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
    payload = _svc_payload(vendor="viptela", sites=[_site()])
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
    payload = _svc_payload(vendor="versa", sites=[_site()])
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
    gen, client = _make_gen(payload, existing_ip=[])
    with patch("generators.generate_sdwan.find_or_create_device", new=AsyncMock()):
        await gen.generate()

    ip_calls = [c for c in client.create.await_args_list if c.kwargs.get("kind") == "IpamIPAddress"]
    assert ip_calls, "Expected an IpamIPAddress create"
    assert ip_calls[0].kwargs["address"] == "10.42.7.1/24"


@pytest.mark.asyncio
async def test_existing_lan_address_not_recreated() -> None:
    """When the LAN IP already exists (filters returns it), it is reused — no create."""
    payload = _svc_payload(sites=[_site()])
    gen, client = _make_gen(payload, existing_ip=[MagicMock(id="ip-existing")])
    with patch("generators.generate_sdwan.find_or_create_device", new=AsyncMock()):
        await gen.generate()
    assert not any(
        c for c in client.create.await_args_list if c.kwargs.get("kind") == "IpamIPAddress"
    )


@pytest.mark.asyncio
async def test_edge_materialised_via_find_or_create() -> None:
    """The edge is always materialised through find_or_create_device (idempotent by name)."""
    payload = _svc_payload(sites=[_site(name="paris")])
    gen, _client = _make_gen(payload)
    with patch("generators.generate_sdwan.find_or_create_device", new=AsyncMock()) as foc:
        foc.return_value = MagicMock(id="edge-new")
        await gen.generate()
        foc.assert_awaited_once()
        assert foc.await_args.kwargs["name"] == "treasury-ops-paris-edge"
