"""Create L3VPN wizard form."""

from __future__ import annotations

import os
import urllib.request
import uuid
from typing import Any

import streamlit as st
from utils import client_for, run_async
from utils.validators import validate_create_l3vpn_form

st.title("Create L3VPN")

client_main = client_for()
tenants = run_async(client_main.all(kind="OrganizationTenant"))
tenant_names = sorted(t.name.value for t in tenants)

pes = run_async(
    client_main.filters(kind="DcimDevice", role__value="pe", prefetch_relationships=True)
)
pe_options = {f"{p.name.value} ({p.platform.peer.name.value})": p.name.value for p in pes}

with st.form("create_l3vpn"):
    st.subheader("Service basics")
    name = st.text_input("Name", placeholder="acme-prod")
    description = st.text_input("Description (optional)")
    tenant = st.selectbox("Tenant", options=tenant_names)
    address_family = st.radio("Address family", options=["ipv4", "ipv4_ipv6"], horizontal=True)

    st.subheader("Sites")
    site_count = st.number_input("Number of sites", min_value=2, max_value=4, value=2, step=1)
    sites: list[dict[str, Any]] = []
    for i in range(int(site_count)):
        st.markdown(f"**Site {i + 1}**")
        site_name = st.text_input("Site name", key=f"sname_{i}")
        pe_label = st.selectbox("PE", options=list(pe_options.keys()), key=f"pe_{i}")
        subnet = st.text_input("Customer subnet (CIDR)", key=f"sub_{i}", placeholder="10.1.0.0/24")
        proto = st.radio(
            "PE-CE routing",
            options=["ebgp", "static", "connected"],
            key=f"proto_{i}",
            horizontal=True,
        )
        asn = (
            st.number_input(
                "BGP peer ASN", min_value=0, max_value=4294967295, key=f"asn_{i}", value=0
            )
            if proto == "ebgp"
            else None
        )
        static_routes = None
        if proto == "static":
            static_text = st.text_area(
                "Static routes (one `<prefix> via <next-hop>` per line)", key=f"sr_{i}"
            )
            static_routes = []
            for line in static_text.splitlines():
                parts = [p.strip() for p in line.split("via")]
                if len(parts) == 2:
                    static_routes.append({"prefix": parts[0], "next_hop": parts[1]})

        sites.append(
            {
                "name": site_name,
                "pe": pe_options[pe_label],
                "customer_subnet": subnet,
                "routing_protocol": proto,
                "bgp_peer_asn": int(asn) if asn else None,
                "static_routes": static_routes,
            }
        )

    submitted = st.form_submit_button("Create L3VPN", type="primary")

if submitted:
    errors = validate_create_l3vpn_form(name=name, tenant=tenant, sites=sites)
    if errors:
        for e in errors:
            st.error(e)
        st.stop()

    with st.spinner("Opening branch and creating objects..."):
        branch_name = f"service/l3vpn-{uuid.uuid4().hex[:8]}"
        branch = run_async(client_main.branch.create(branch_name, sync_with_git=False))
        client = client_for(branch=branch_name)

        vpn_id_pool = run_async(client.get(kind="CoreNumberPool", name__value="vpn_id_pool"))
        # `l3vpns` is the target group the `generate_l3vpn` generator runs
        # against; without membership the catalog-created VPN is invisible
        # to the generator and downstream artifact/check pipeline.
        l3vpns_group = run_async(client.get(kind="CoreStandardGroup", name__value="l3vpns"))

        vpn = run_async(
            client.create(
                kind="ServiceL3Vpn",
                name=name,
                description=description,
                vpn_id=vpn_id_pool,
                address_family=address_family,
                tenant={"hfid": [tenant]},
                member_of_groups=[l3vpns_group.id],
            )
        )
        run_async(vpn.save())
        vpn_id = int(vpn.vpn_id.value)

        for s in sites:
            cust = run_async(
                client.create(
                    kind="IpamPrefix",
                    prefix=s["customer_subnet"],
                    status="active",
                    role="public",
                )
            )
            run_async(cust.save())
            site_obj = run_async(
                client.create(
                    kind="ServiceL3VpnSite",
                    name=s["name"],
                    l3vpn=vpn,
                    pe_device={"hfid": [s["pe"]]},
                    customer_subnet=cust,
                    routing_protocol=s["routing_protocol"],
                    bgp_peer_asn=s["bgp_peer_asn"],
                    static_routes=s["static_routes"],
                )
            )
            run_async(site_obj.save())

        # Run the L3VPN generator explicitly on the branch and wait for it to
        # finish, *before* triggering artifact rendering. The generator
        # materializes the VRF / interfaces / IPs the artifact templates depend
        # on; if we render before it runs, downstream artifacts use stale data
        # and the proposed change shows no diff against main.
        #
        # We trigger the generator the same way bootstrap does
        # (scripts/run_generator.py) instead of relying on automatic dispatch:
        # in this wizard the generator otherwise only runs inside the
        # proposed-change pipeline, which we open last — too late for the
        # artifact step below. ``wait_until_completion`` blocks until the VRF
        # and per-site IPs exist, so artifacts render against complete data.
        generator = run_async(
            client.get(kind="CoreGeneratorDefinition", name__value="generate_l3vpn")
        )
        run_async(
            client.execute_graphql(
                """
                mutation RunGenerator($id: String!) {
                  CoreGeneratorDefinitionRun(
                    data: {id: $id}, wait_until_completion: true
                  ) {
                    ok
                  }
                }
                """,
                variables={"id": generator.id},
                branch_name=branch_name,
            )
        )

        # Trigger artifact regeneration on the branch so the proposed change
        # shows real per-PE config diffs. Infrahub doesn't automatically
        # re-render artifacts whose template's query data changed; we have
        # to nudge each definition.
        for definition in run_async(client.all(kind="CoreArtifactDefinition")):
            url = f"{client.address}/api/artifact/generate/{definition.id}?branch={branch_name}"
            request = urllib.request.Request(
                url,
                method="POST",
                headers={"X-INFRAHUB-KEY": os.environ["INFRAHUB_API_TOKEN"]},
            )
            urllib.request.urlopen(request).read()

        pc = run_async(
            client_main.create(
                kind="CoreProposedChange",
                source_branch=branch_name,
                destination_branch="main",
                name=f"Create L3VPN {name}",
            )
        )
        run_async(pc.save())

    ui_url = os.environ.get("INFRAHUB_UI_URL", "http://localhost:8000")
    st.success(f"Branch `{branch_name}` opened, vpn_id={vpn_id}.")
    st.markdown(
        f"**Next step:** review the diff and the validation pipeline in Infrahub, "
        f"then merge the proposed change.\n\n"
        f"- [Open Proposed Change]({ui_url}/proposed-changes/{pc.id})\n"
        f"- [Browse branch in Infrahub]({ui_url}/?branch={branch_name})",
    )
