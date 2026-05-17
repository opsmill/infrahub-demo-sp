"""Create SD-WAN service wizard form."""

from __future__ import annotations

import os
import time
import urllib.request
import uuid
from typing import Any

import streamlit as st
from utils import client_for, run_async
from utils.validators import validate_create_sdwan_form

st.title("Create SD-WAN service")

client_main = client_for()
tenants = run_async(client_main.all(kind="OrganizationTenant"))
tenant_names = sorted(t.name.value for t in tenants)

locations = run_async(client_main.all(kind="LocationSite"))
location_options = {loc.name.value: loc.shortname.value for loc in locations}

with st.form("create_sdwan"):
    st.subheader("Service basics")
    name = st.text_input("Name", placeholder="acme-overlay")
    description = st.text_input("Description (optional)")
    tenant = st.selectbox("Tenant", options=tenant_names)
    vendor = st.radio("Vendor", options=["viptela", "versa"], horizontal=True)
    topology = st.radio("Topology", options=["full-mesh", "hub-spoke"], horizontal=True)

    st.subheader("Sites")
    site_count = st.number_input("Number of sites", min_value=2, max_value=6, value=2, step=1)
    sites: list[dict[str, Any]] = []
    for i in range(int(site_count)):
        st.markdown(f"**Site {i + 1}**")
        site_name = st.text_input("Site name", key=f"sn_{i}")
        role = st.radio(
            "Role",
            options=["hub", "spoke", "branch"],
            key=f"sr_{i}",
            horizontal=True,
        )
        location_label = st.selectbox(
            "Location", options=list(location_options.keys()), key=f"sloc_{i}"
        )
        lan_subnet = st.text_input(
            "LAN subnet (CIDR)", key=f"slan_{i}", placeholder="10.250.10.0/24"
        )
        sites.append(
            {
                "name": site_name,
                "role": role,
                "location": location_options[location_label],
                "lan_subnet": lan_subnet,
            }
        )

    submitted = st.form_submit_button("Create SD-WAN service", type="primary")

if submitted:
    errors = validate_create_sdwan_form(
        name=name,
        tenant=tenant,
        vendor=vendor,
        topology=topology,
        sites=sites,
    )
    if errors:
        for e in errors:
            st.error(e)
        st.stop()

    with st.spinner("Opening branch and creating objects..."):
        branch_name = f"sdwan/{uuid.uuid4().hex[:8]}"
        branch = run_async(client_main.branch.create(branch_name, sync_with_git=False))
        client = client_for(branch=branch_name)

        sdwan_id_pool = run_async(client.get(kind="CoreNumberPool", name__value="sdwan_id_pool"))
        sdwans_group = run_async(client.get(kind="CoreStandardGroup", name__value="sdwans"))

        svc = run_async(
            client.create(
                kind="ServiceSdwan",
                name=name,
                description=description,
                service_id=sdwan_id_pool,
                vendor=vendor,
                topology=topology,
                tenant={"hfid": [tenant]},
                member_of_groups=[sdwans_group.id],
            )
        )
        run_async(svc.save())
        service_id = int(svc.service_id.value)

        for s in sites:
            lan = run_async(
                client.create(
                    kind="IpamPrefix",
                    prefix=s["lan_subnet"],
                    status="active",
                    role="public",
                )
            )
            run_async(lan.save())
            site_obj = run_async(
                client.create(
                    kind="ServiceSdwanSite",
                    name=s["name"],
                    sdwan=svc,
                    role=s["role"],
                    location={"hfid": [s["location"]]},
                    lan_subnet=lan,
                )
            )
            run_async(site_obj.save())

        # Wait for the SD-WAN generator (auto-fired by group membership) to
        # materialise the edge devices / LAN IPs before kicking off artifact
        # rendering, otherwise per-edge configs render against stale data.
        def _is_active() -> bool:
            v = run_async(client.get(kind="ServiceSdwan", name__value=name))
            return v.status.value == "active"

        deadline = time.monotonic() + 120
        while not _is_active() and time.monotonic() < deadline:
            time.sleep(2)

        # Trigger artifact regeneration on the branch so the proposed change
        # shows real per-edge config diffs. Infrahub doesn't automatically
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
                name=f"Create SD-WAN {name}",
            )
        )
        run_async(pc.save())

    ui_url = os.environ.get("INFRAHUB_UI_URL", "http://localhost:8000")
    st.success(f"Branch `{branch_name}` opened, service_id={service_id}.")
    st.markdown(
        f"**Next step:** review the diff and the validation pipeline in Infrahub, "
        f"then merge the proposed change.\n\n"
        f"- [Open Proposed Change]({ui_url}/proposed-changes/{pc.id})\n"
        f"- [Browse branch in Infrahub]({ui_url}/?branch={branch_name})",
    )
