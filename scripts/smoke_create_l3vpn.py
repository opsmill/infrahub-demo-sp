"""Smoke test: create a single ServiceL3Vpn with one site via the SDK."""

from __future__ import annotations

import asyncio

from infrahub_sdk.client import InfrahubClient


async def main() -> None:
    """Allocate vpn_id, create VPN + site, print result."""
    client = InfrahubClient(
        address="http://localhost:8000",
        api_token="06438eb2-8019-4776-878c-0941b1f1d1ec",
    )
    pool = await client.get(kind="CoreNumberPool", name__value="vpn_id_pool")
    vpn_id = int(await pool.allocate_resource(identifier="smoketest"))

    cust = await client.create(
        kind="IpamPrefix",
        prefix="192.168.1.0/24",
        status="active",
        role="public",
    )
    await cust.save()

    vpn = await client.create(
        kind="ServiceL3Vpn",
        name="smoketest-vpn",
        vpn_id=vpn_id,
        tenant={"hfid": ["acme"]},
    )
    await vpn.save()

    site = await client.create(
        kind="ServiceL3VpnSite",
        name="smoketest-site-lon",
        l3vpn=vpn,
        pe={"hfid": ["pe-lon-arista"]},
        customer_subnet=cust,
        routing_protocol="ebgp",
        bgp_peer_asn=65501,
    )
    await site.save()

    print(f"ServiceL3Vpn id={vpn.id}, vpn_id={vpn_id}")


if __name__ == "__main__":
    asyncio.run(main())
