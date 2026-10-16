"""Add PE wizard: onboard a new PE router to an MPLS backbone as a proposed change."""

from __future__ import annotations

import os
import time
import urllib.request
import uuid

import streamlit as st
from utils import client_for, run_async
from utils.validators import (
    PE_ONBOARDING_PROGRESS_QUERY,
    PE_PLATFORMS,
    pe_is_materialised,
    validate_add_pe_form,
)

# How many consecutive materialised readings end the wait. See the poll loop.
REQUIRED_STABLE_PASSES = 2
# The iBGP mesh is 2 x (N - 1) session rows, so this generator takes longer than
# the L3VPN one; the default wait is sized for a nine-PE backbone.
GENERATOR_WAIT_SECONDS = int(os.environ.get("GENERATOR_WAIT_TIME", "180"))
# Per artifact definition. Without a timeout one slow render blocks the page.
ARTIFACT_REQUEST_TIMEOUT_SECONDS = 60

st.title("Add PE")
st.caption(
    "Adds a PE router to a backbone on a new branch: loopback, core links to the "
    "uplinks you pick, IS-IS, LDP, MP-BGP and the iBGP full mesh, then opens a "
    "proposed change."
)

client_main = client_for()

pes = run_async(
    client_main.filters(kind="DcimDevice", role__value="pe", prefetch_relationships=True)
)
pe_name_by_id = {pe.id: pe.name.value for pe in pes}
existing_device_names = {d.name.value for d in run_async(client_main.all(kind="DcimDevice"))}

backbones = run_async(client_main.all(kind="TopologyMplsBackbone"))
backbone_pes: dict[str, list[str]] = {}
for backbone in backbones:
    run_async(backbone.pes.fetch())
    backbone_pes[backbone.name.value] = sorted(
        pe_name_by_id[peer.id] for peer in backbone.pes.peers if peer.id in pe_name_by_id
    )

sites = run_async(client_main.all(kind="LocationSite"))
site_options = {f"{s.name.value} ({s.shortname.value})": s.shortname.value for s in sites}

platforms = run_async(client_main.all(kind="DcimPlatform"))
platform_options = {}
for platform in platforms:
    if platform.name.value not in PE_PLATFORMS:
        continue
    lab = platform.containerlab_os.value if platform.containerlab_os else None
    label = f"{platform.name.value} ({'lab image: ' + lab if lab else 'no lab image'})"
    platform_options[label] = platform.name.value

device_types = sorted(dt.name.value for dt in run_async(client_main.all(kind="DcimDeviceType")))

# The backbone select sits outside the form so changing it refreshes the uplink list.
backbone_name = st.selectbox("Backbone", options=sorted(backbone_pes))
candidate_uplinks = backbone_pes.get(backbone_name, [])

with st.form("add_pe"):
    st.subheader("New PE")
    name = st.text_input("Name", placeholder="pe-09")
    description = st.text_input("Description (optional)")
    site_label = st.selectbox("Site", options=sorted(site_options))
    platform_label = st.selectbox("Platform", options=sorted(platform_options))
    device_type = st.selectbox("Device type", options=device_types)

    st.subheader("Uplinks")
    uplinks = st.multiselect(
        "Existing PEs to link to (at least two)",
        options=candidate_uplinks,
        max_selections=4,
    )

    submitted = st.form_submit_button("Add PE", type="primary")

if submitted:
    errors = validate_add_pe_form(
        name=name,
        platform=platform_options[platform_label],
        existing_device_names=existing_device_names,
        uplinks=uplinks,
        backbone_pes=set(candidate_uplinks),
    )
    if errors:
        for error in errors:
            st.error(error)
        st.stop()

    expected_sessions = len(candidate_uplinks)

    with st.spinner("Opening branch and creating the onboarding request..."):
        branch_name = f"backbone/add-pe-{name}-{uuid.uuid4().hex[:8]}"
        run_async(client_main.branch.create(branch_name, sync_with_git=False))
        client = client_for(branch=branch_name)

        # `pe_onboardings` is the target group of generate_pe. Fetched without
        # `include=["members"]` on purpose: the add below is a RelationshipAdd
        # mutation, which needs only the group's id.
        group = run_async(client.get(kind="CoreStandardGroup", name__value="pe_onboardings"))

        request = run_async(
            client.create(
                kind="TopologyPeOnboarding",
                name=name,
                description=description,
                backbone={"hfid": [backbone_name]},
                location={"hfid": [site_options[site_label]]},
                platform={"hfid": [platform_options[platform_label]]},
                device_type={"hfid": [device_type]},
                uplinks=[{"hfid": [uplink]} for uplink in uplinks],
            )
        )
        run_async(request.save())

        # Joining the group fires `trigger-pe-generator`
        # (objects/events/00_triggers.yml), which runs generate_pe on this branch.
        # RelationshipAdd rather than `members.add()` + `save()`, for the reason
        # recorded in 1_Create_L3VPN.py: a save replaces the whole member list.
        run_async(group.add_relationships(relation_to_update="members", related_nodes=[request.id]))

        def _materialised() -> bool:
            result = run_async(
                client.execute_graphql(
                    query=PE_ONBOARDING_PROGRESS_QUERY,
                    variables={"name": name},
                    branch_name=branch_name,
                )
            )
            edges = (result.get("TopologyPeOnboarding") or {}).get("edges") or []
            return pe_is_materialised(
                edges[0]["node"] if edges else None,
                core_interface_count=int((result.get("InterfacePhysical") or {}).get("count", 0)),
                ibgp_session_count=int((result.get("RoutingBGPSession") or {}).get("count", 0)),
                expected_uplinks=len(set(uplinks)),
                expected_sessions=expected_sessions,
            )

        # Poll the generator's output, not a task: the trigger dispatches it
        # server-side, so there is no task id to hold. Two consecutive passes
        # so a still-settling run cannot slip through between polls.
        deadline = time.monotonic() + GENERATOR_WAIT_SECONDS
        stable = 0
        while stable < REQUIRED_STABLE_PASSES and time.monotonic() < deadline:
            stable = stable + 1 if _materialised() else 0
            if stable < REQUIRED_STABLE_PASSES:
                time.sleep(3)
        if stable < REQUIRED_STABLE_PASSES:
            st.error(
                f"The PE generator did not finish within {GENERATOR_WAIT_SECONDS}s, so the "
                "device, links and iBGP sessions may be incomplete. No proposed change "
                f"was opened. Branch `{branch_name}` is preserved for inspection. "
                "Check that `trigger-pe-generator` exists "
                "(objects/events/00_triggers.yml is loaded by `invoke bootstrap`) and see "
                "the task-worker logs: `docker compose -p sp-demo logs task-worker --tail 200`."
            )
            st.stop()

        # Re-render every artifact on the branch so the proposed change shows the
        # new PE's config, every neighbour's new session and the lab topology.
        definitions = run_async(client.all(kind="CoreArtifactDefinition"))
        api_token = os.environ.get("INFRAHUB_API_TOKEN")
        if definitions and not api_token:
            st.error(
                "INFRAHUB_API_TOKEN is not set, so the artifacts cannot be re-rendered. "
                f"No proposed change was opened. Branch `{branch_name}` is preserved."
            )
            st.stop()
        for definition in definitions:
            url = f"{client.address}/api/artifact/generate/{definition.id}?branch={branch_name}"
            request_obj = urllib.request.Request(
                url, method="POST", headers={"X-INFRAHUB-KEY": api_token or ""}
            )
            try:
                with urllib.request.urlopen(
                    request_obj, timeout=ARTIFACT_REQUEST_TIMEOUT_SECONDS
                ) as response:
                    response.read()
            except OSError as exc:
                st.error(
                    f"Re-rendering artifact definition {definition.name.value} failed: {exc}. "
                    f"No proposed change was opened. Branch `{branch_name}` is preserved."
                )
                st.stop()

        pc = run_async(
            client_main.create(
                kind="CoreProposedChange",
                source_branch=branch_name,
                destination_branch="main",
                name=f"Add PE {name}",
            )
        )
        run_async(pc.save())

    ui_url = os.environ.get("INFRAHUB_UI_URL", "http://localhost:8000")
    st.success(f"Branch `{branch_name}` opened for {name} with uplinks {', '.join(uplinks)}.")
    st.markdown(
        "**Next step:** review the diff and the validation pipeline in Infrahub, "
        "then merge the proposed change. To boot the new PE in containerlab before "
        f"the merge, run `uv run invoke lab.deploy --branch {branch_name}`. After the "
        "merge, `uv run invoke lab.deploy` boots it from main.\n\n"
        f"- [Open Proposed Change]({ui_url}/proposed-changes/{pc.id})\n"
        f"- [Browse branch in Infrahub]({ui_url}/?branch={branch_name})",
    )
