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
    vpn_id_pool = await client.get(kind="CoreNumberPool", name__value="vpn_id_pool")

    # Customer prefixes live in a namespace per VPN, the same way the catalog
    # creates them (service_catalog/pages/1_Create_L3VPN.py) and the generator
    # expects them (generate_l3vpn.py: _customer_namespace). Creating this
    # prefix in `default` instead would leave it outside the namespace the
    # generator puts the LAN gateway in.
    customer_ns = await client.create(
        kind="IpamNamespace",
        name="vrf-smoketest-vpn",
        description="Customer address space for L3VPN smoketest-vpn.",
    )
    await customer_ns.save(allow_upsert=True)

    cust = await client.create(
        kind="IpamPrefix",
        prefix="192.168.1.0/24",
        status="active",
        role="public",
        ip_namespace=customer_ns,
    )
    await cust.save(allow_upsert=True)

    vpn = await client.create(
        kind="ServiceL3Vpn",
        name="smoketest-vpn",
        vpn_id=vpn_id_pool,
        tenant={"hfid": ["markets-trading"]},
    )
    await vpn.save()
    vpn_id = int(vpn.vpn_id.value)

    site = await client.create(
        kind="ServiceL3VpnSite",
        name="smoketest-site-lon",
        l3vpn=vpn,
        pe_device={"hfid": ["pe-01"]},
        customer_subnet=cust,
        routing_protocol="ebgp",
        # No bgp_peer_asn: exercise the default path, where the generator
        # allocates this VPN's customer AS from customer_asn_pool.
    )
    await site.save()

    print(f"ServiceL3Vpn id={vpn.id}, vpn_id={vpn_id}")


if __name__ == "__main__":
    asyncio.run(main())
