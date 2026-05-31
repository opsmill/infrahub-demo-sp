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

    with st.spinner("Creating objects, running the generator, and rendering configs..."):
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

        # Open the proposed change FIRST. The SD-WAN generator does not run on
        # plain branch writes — it only runs inside the proposed-change pipeline
        # (``proposed-changed-run-generator``). The pipeline materialises one
        # edge + LAN IP per site, adds each edge to the vendor edge group, and
        # flips the service to ``active`` as its final step. Creating the PC up
        # front is what triggers that run; an earlier ordering that waited for
        # ``active`` *before* creating the PC dead-locked (the generator could
        # never run, so the wait always timed out).
        pc = run_async(
            client_main.create(
                kind="CoreProposedChange",
                source_branch=branch_name,
                destination_branch="main",
                name=f"Create SD-WAN {name}",
            )
        )
        run_async(pc.save())

        # Artifact rendering MUST then be triggered explicitly — the PC pipeline
        # only re-renders artifacts that already exist on ``main`` (to compute
        # config diffs); it does NOT create artifacts for brand-new targets, and
        # its own artifact-validation step races ahead of the generator's group
        # write anyway. The wizard's edges are brand-new ``DcimDevice`` members
        # of the vendor edge group with no artifact on ``main``, so we wait for
        # the generator to finish (service ``active`` => group populated), then
        # re-POST the vendor definition on a short cadence until one artifact per
        # edge appears. Mirrors ``scripts/regenerate_artifacts.py``.
        sdwan_def_name = "sdwan-viptela-config" if vendor == "viptela" else "sdwan-versa-config"
        sdwan_def = next(
            d
            for d in run_async(client.all(kind="CoreArtifactDefinition"))
            if d.name.value == sdwan_def_name
        )
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
            svc_now = run_async(client.get(kind="ServiceSdwan", name__value=name))
            return svc_now.status.value == "active"

        def _sdwan_artifact_count() -> int:
            arts = run_async(client.filters(kind="CoreArtifact", definition__ids=[sdwan_def.id]))
            return len(arts)

        # The PC pipeline takes a couple of minutes to start and run the
        # generator; a generous deadline keeps the wizard from giving up before
        # the configs land, with a UI-retrigger fallback on timeout.
        deadline = time.monotonic() + 420
        generator_done = False
        last_post = 0.0
        while time.monotonic() < deadline:
            if not generator_done:
                if not _service_active():
                    time.sleep(5)
                    continue
                generator_done = True
            if _sdwan_artifact_count() >= expected_edge_count:
                break
            # Re-POST on a short cadence once the generator is done; each attempt
            # is a no-op until the vendor group's new members are committed and
            # resolvable as definition targets, after which it renders one
            # artifact per edge.
            if time.monotonic() - last_post > 8:
                _post_definition(sdwan_def.id)
                last_post = time.monotonic()
            time.sleep(3)
        else:
            st.warning(
                f"Only {_sdwan_artifact_count()} of {expected_edge_count} "
                f"`{sdwan_def_name}` artifacts had rendered when polling stopped. "
                "The proposed change is open anyway — re-trigger artifact "
                "generation from its Artifacts tab if any are missing."
            )

    ui_url = os.environ.get("INFRAHUB_UI_URL", "http://localhost:8000")
    st.success(f"Branch `{branch_name}` opened, service_id={service_id}.")
    st.markdown(
        f"**Next step:** review the rendered configs on the Artifacts tab, plus "
        f"the diff and validation pipeline, then merge the proposed change.\n\n"
        f"- [Open Proposed Change]({ui_url}/proposed-changes/{pc.id})\n"
        f"- [Browse branch in Infrahub]({ui_url}/?branch={branch_name})",
    )
