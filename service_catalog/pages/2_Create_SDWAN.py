"""Create SD-WAN intent wizard form.

Creates a ServiceSdwanIntent on a fresh proposed-change branch. The
generator (registered with execute_in_proposed_change=true) fires
automatically — allocates service_id from the band-scoped pool,
materialises one edge device per site, assigns LAN IPs, and links the
realised ServiceSdwan back. The catalog polls until the intent flips
to `active` (or `failed`), kicks off artifact rendering, and opens
the proposed change.
"""

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
    band = st.radio(
        "Pool band",
        options=["financial", "isp", "internal"],
        horizontal=True,
        help="Selects which service_id pool the generator draws from.",
    )
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

    submitted = st.form_submit_button("Create SD-WAN intent", type="primary")

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
    # branch — saves the user from orphaned half-created intents.
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

    with st.spinner("Opening branch and creating intent..."):
        branch_name = f"sdwan/{uuid.uuid4().hex[:8]}"
        branch = run_async(client_main.branch.create(branch_name, sync_with_git=False))
        client = client_for(branch=branch_name)

        intents_group = run_async(
            client.get(kind="CoreGeneratorGroup", name__value="sdwan_intents")
        )

        intent = run_async(
            client.create(
                kind="ServiceSdwanIntent",
                name=name,
                description=description,
                band=band,
                vendor=vendor,
                topology=topology,
                tenant={"hfid": [tenant]},
                member_of_groups=[intents_group.id],
            )
        )
        run_async(intent.save())

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
                    kind="ServiceSdwanIntentSite",
                    name=s["name"],
                    intent=intent,
                    role=s["role"],
                    location={"hfid": [s["location"]]},
                    lan_subnet=lan,
                )
            )
            run_async(site_obj.save())

        # Poll the intent for active/failed.
        def _intent_status() -> tuple[str, str | None]:
            i = run_async(client.get(kind="ServiceSdwanIntent", name__value=name))
            return i.status.value, i.failure_message.value or None

        deadline = time.monotonic() + 120
        status, failure = _intent_status()
        while status not in {"active", "failed"} and time.monotonic() < deadline:
            time.sleep(2)
            status, failure = _intent_status()

        if status == "failed":
            st.error(f"Generator reported failure: {failure or 'no message'}")
            st.stop()

        # Trigger artifact regeneration on the branch so the proposed change
        # shows real per-edge config diffs.
        sdwan_edge_group = "sdwan_viptela_edges" if vendor == "viptela" else "sdwan_versa_edges"
        sdwan_def_name = "sdwan-viptela-config" if vendor == "viptela" else "sdwan-versa-config"
        artifact_definitions = run_async(client.all(kind="CoreArtifactDefinition"))

        def _post_definition(def_id: str) -> None:
            url = f"{client.address}/api/artifact/generate/{def_id}?branch={branch_name}"
            request = urllib.request.Request(
                url,
                method="POST",
                headers={"X-INFRAHUB-KEY": os.environ["INFRAHUB_API_TOKEN"]},
            )
            urllib.request.urlopen(request).read()

        for definition in artifact_definitions:
            _post_definition(definition.id)

        # Confirm the per-edge artifacts actually materialised.
        edge_group_obj = run_async(
            client.get(kind="CoreStandardGroup", name__value=sdwan_edge_group)
        )
        run_async(edge_group_obj.members.fetch())
        expected_edge_count = len(edge_group_obj.members.peers)
        sdwan_def = next(d for d in artifact_definitions if d.name.value == sdwan_def_name)

        def _count_sdwan_artifacts() -> int:
            return len(
                run_async(
                    client.filters(
                        kind="CoreArtifact",
                        definition__ids=[sdwan_def.id],
                    )
                )
            )

        deadline = time.monotonic() + 90
        reposted = False
        while time.monotonic() < deadline:
            if _count_sdwan_artifacts() >= expected_edge_count:
                break
            time.sleep(3)
            if not reposted and time.monotonic() > deadline - 60:
                _post_definition(sdwan_def.id)
                reposted = True
        else:
            st.warning(
                f"Only {_count_sdwan_artifacts()} of {expected_edge_count} "
                f"`{sdwan_def_name}` artifacts had materialised when we gave up "
                "polling. The proposed change will open anyway — re-trigger "
                "artifact generation from the Infrahub UI if any are missing."
            )

        intent_final = run_async(client.get(kind="ServiceSdwanIntent", name__value=name))
        run_async(intent_final.realised_service.fetch())
        realised = intent_final.realised_service.peer
        service_id = int(realised.service_id.value) if realised else None

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
    sid_msg = f", service_id={service_id}" if service_id is not None else ""
    st.success(f"Branch `{branch_name}` opened{sid_msg}.")
    st.markdown(
        f"**Next step:** review the diff and the validation pipeline in Infrahub, "
        f"then merge the proposed change.\n\n"
        f"- [Open Proposed Change]({ui_url}/proposed-changes/{pc.id})\n"
        f"- [Browse branch in Infrahub]({ui_url}/?branch={branch_name})",
    )
