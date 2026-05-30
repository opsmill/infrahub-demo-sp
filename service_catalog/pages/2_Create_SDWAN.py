"""Create SD-WAN service wizard form."""

from __future__ import annotations

import os
import time
import urllib.request
import uuid
from typing import Any

import streamlit as st
from infrahub_sdk.exceptions import GraphQLError
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

    # Pre-flight: refuse subnets that already exist in Infrahub before opening a
    # branch — saves the user from orphaned half-created services. Belt and
    # braces: the try/except below catches the same collision on a real race.
    requested_subnets = [s["lan_subnet"] for s in sites]
    existing_prefixes = run_async(
        client_main.filters(kind="IpamPrefix", prefix__values=requested_subnets)
    )
    existing_subnets = {p.prefix.value for p in existing_prefixes}
    collisions = [s for s in requested_subnets if s in existing_subnets]
    if collisions:
        for c in collisions:
            st.error(
                f"LAN subnet `{c}` already exists in Infrahub (probably from a "
                "bootstrap-seeded service). Pick a different CIDR, or delete the "
                "existing IpamPrefix first."
            )
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
            try:
                run_async(lan.save())
            except GraphQLError as exc:
                if "prefix-ip_namespace" in str(exc):
                    st.error(
                        f"LAN subnet `{s['lan_subnet']}` for site `{s['name']}` already "
                        "exists in Infrahub (probably from a bootstrap-seeded service). "
                        "Pick a different CIDR, or delete the existing IpamPrefix first."
                    )
                    st.stop()
                raise
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

        # The SD-WAN generator is auto-dispatched when the service joins the
        # ``sdwans`` group; it materialises one edge device + LAN IP per site,
        # adds each edge to the vendor edge group, and flips the service to
        # ``active`` as its final step. Artifacts target that vendor group, so
        # we must wait for the generator before rendering — and then keep
        # re-triggering generation until one config artifact per site exists,
        # because /api/artifact/generate is async and a no-op against a group
        # that is still empty (the generator and the artifact request race).
        sdwan_def_name = "sdwan-viptela-config" if vendor == "viptela" else "sdwan-versa-config"
        artifact_definitions = run_async(client.all(kind="CoreArtifactDefinition"))
        sdwan_def = next(d for d in artifact_definitions if d.name.value == sdwan_def_name)
        expected_edge_count = len(sites)

        def _post_definition(def_id: str) -> None:
            url = f"{client.address}/api/artifact/generate/{def_id}?branch={branch_name}"
            request = urllib.request.Request(
                url,
                method="POST",
                headers={"X-INFRAHUB-KEY": os.environ["INFRAHUB_API_TOKEN"]},
            )
            urllib.request.urlopen(request).read()

        def _service_active() -> bool:
            v = run_async(client.get(kind="ServiceSdwan", name__value=name))
            return v.status.value == "active"

        def _count_sdwan_artifacts() -> int:
            return len(
                run_async(client.filters(kind="CoreArtifact", definition__ids=[sdwan_def.id]))
            )

        deadline = time.monotonic() + 240
        posted_all = False
        last_post = 0.0
        while time.monotonic() < deadline:
            if not posted_all:
                if not _service_active():
                    time.sleep(3)
                    continue
                # Generator finished (edges exist) — render every definition once.
                for definition in artifact_definitions:
                    _post_definition(definition.id)
                posted_all = True
                last_post = time.monotonic()
            if _count_sdwan_artifacts() >= expected_edge_count:
                break
            # Re-poke the SD-WAN definition periodically until the per-edge
            # artifacts show up (covers read-replica lag on group membership).
            if time.monotonic() - last_post > 20:
                _post_definition(sdwan_def.id)
                last_post = time.monotonic()
            time.sleep(3)
        else:
            st.warning(
                f"Only {_count_sdwan_artifacts()} of {expected_edge_count} "
                f"`{sdwan_def_name}` artifacts had materialised when we gave up "
                "polling. The proposed change will open anyway — re-trigger "
                "artifact generation from the Infrahub UI if any are missing."
            )

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
