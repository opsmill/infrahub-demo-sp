"""Create L3VPN intent wizard form.

Creates a ServiceL3VpnIntent on a fresh proposed-change branch. The
generator (registered with execute_in_proposed_change=true) fires
automatically against the new intent — allocating vpn_id from the
band-scoped pool, materialising VRF/RTs/PE-CE interfaces/IPs, and
linking the realised ServiceL3Vpn back. The catalog polls until the
intent flips to `active` (or `failed`) and then opens the proposed
change for review.
"""

from __future__ import annotations

import os
import time
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
    band = st.radio(
        "Pool band",
        options=["financial", "isp", "internal"],
        horizontal=True,
        help="Selects which vpn_id pool the generator draws from.",
    )
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

    submitted = st.form_submit_button("Create L3VPN intent", type="primary")

if submitted:
    errors = validate_create_l3vpn_form(name=name, tenant=tenant, sites=sites)
    if errors:
        for e in errors:
            st.error(e)
        st.stop()

    with st.spinner("Opening branch and creating intent..."):
        branch_name = f"service/l3vpn-{uuid.uuid4().hex[:8]}"
        branch = run_async(client_main.branch.create(branch_name, sync_with_git=False))
        client = client_for(branch=branch_name)

        # The generator picks up new intents via this group's membership.
        intents_group = run_async(
            client.get(kind="CoreGeneratorGroup", name__value="l3vpn_intents")
        )

        intent = run_async(
            client.create(
                kind="ServiceL3VpnIntent",
                name=name,
                description=description,
                band=band,
                address_family=address_family,
                tenant={"hfid": [tenant]},
                member_of_groups=[intents_group.id],
            )
        )
        run_async(intent.save())

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
                    kind="ServiceL3VpnIntentSite",
                    name=s["name"],
                    intent=intent,
                    pe_device={"hfid": [s["pe"]]},
                    customer_subnet=cust,
                    routing_protocol=s["routing_protocol"],
                    bgp_peer_asn=s["bgp_peer_asn"],
                    static_routes=s["static_routes"],
                )
            )
            run_async(site_obj.save())

        # Wait for the generator to finish materialising the realised
        # service. The intent flips to `active` (success) or `failed`
        # (with failure_message set). Don't kick off artifact rendering
        # if the intent failed — surface the error instead.
        def _intent_status() -> tuple[str, str | None]:
            i = run_async(client.get(kind="ServiceL3VpnIntent", name__value=name))
            return i.status.value, i.failure_message.value or None

        deadline = time.monotonic() + 120
        status, failure = _intent_status()
        while status not in {"active", "failed"} and time.monotonic() < deadline:
            time.sleep(2)
            status, failure = _intent_status()

        if status == "failed":
            st.error(f"Generator reported failure: {failure or 'no message'}")
            st.stop()

        # Trigger artifact regeneration so the proposed change shows
        # real per-PE config diffs.
        for definition in run_async(client.all(kind="CoreArtifactDefinition")):
            url = f"{client.address}/api/artifact/generate/{definition.id}?branch={branch_name}"
            request = urllib.request.Request(
                url,
                method="POST",
                headers={"X-INFRAHUB-KEY": os.environ["INFRAHUB_API_TOKEN"]},
            )
            urllib.request.urlopen(request).read()

        # Pull the realised service so we can show the user the vpn_id.
        intent_final = run_async(client.get(kind="ServiceL3VpnIntent", name__value=name))
        run_async(intent_final.realised_service.fetch())
        realised = intent_final.realised_service.peer
        vpn_id = int(realised.vpn_id.value) if realised else None

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
    vpn_msg = f", vpn_id={vpn_id}" if vpn_id is not None else ""
    st.success(f"Branch `{branch_name}` opened{vpn_msg}.")
    st.markdown(
        f"**Next step:** review the diff and the validation pipeline in Infrahub, "
        f"then merge the proposed change.\n\n"
        f"- [Open Proposed Change]({ui_url}/proposed-changes/{pc.id})\n"
        f"- [Browse branch in Infrahub]({ui_url}/?branch={branch_name})",
    )
