"""Create L3VPN wizard form."""

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
        # 0 means "not specified": the L3VPN generator then allocates this
        # VPN's customer AS from customer_asn_pool. A non-zero value is a
        # per-site override for peering with a pre-agreed customer AS.
        asn = (
            st.number_input(
                "BGP peer ASN (0 = allocate from customer_asn_pool)",
                min_value=0,
                max_value=4294967295,
                key=f"asn_{i}",
                value=0,
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
        # against. Membership is added *after* the sites exist (below) so the
        # group-membership trigger fires the generator with complete data.
        # Fetched without `include=["members"]` on purpose: the add below is a
        # RelationshipAdd mutation, which needs only the group's id.
        l3vpns_group = run_async(client.get(kind="CoreStandardGroup", name__value="l3vpns"))

        vpn = run_async(
            client.create(
                kind="ServiceL3Vpn",
                name=name,
                description=description,
                vpn_id=vpn_id_pool,
                address_family=address_family,
                tenant={"hfid": [tenant]},
            )
        )
        run_async(vpn.save())
        vpn_id = int(vpn.vpn_id.value)

        # One IPAM namespace per VPN holds this customer's address space, so two
        # services may request the same private prefix without resolving to the
        # same IpamPrefix row and fighting over its VRF. The generator binds this
        # same namespace to the VRF it creates and puts each site's LAN gateway
        # in it. The name mirrors generators.common.ip_namespace_name, spelled
        # out here because this Streamlit app cannot import from generators/ —
        # keep the two in step.
        customer_ns = run_async(
            client.create(
                kind="IpamNamespace",
                name=f"vrf-{name}",
                description=f"Customer address space for L3VPN {name}.",
            )
        )
        run_async(customer_ns.save(allow_upsert=True))

        for s in sites:
            cust = run_async(
                client.create(
                    kind="IpamPrefix",
                    prefix=s["customer_subnet"],
                    status="active",
                    role="public",
                    ip_namespace=customer_ns,
                )
            )
            # allow_upsert: IpamPrefix is unique on [prefix__value, ip_namespace],
            # so two sites of this VPN naming the same subnet would otherwise
            # raise here — after the branch and the VPN row were already created,
            # leaving a half-built branch behind.
            run_async(cust.save(allow_upsert=True))
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

        # Add the VPN to the `l3vpns` group now that its sites exist. This
        # membership change fires the `trigger-l3vpn-generator` group trigger
        # (objects/events/00_triggers.yml), which runs generate_l3vpn on this
        # branch against the complete VPN+sites data.
        #
        # RelationshipAdd rather than `members.add()` + `save()`: saving the
        # group re-sends its entire member list as a replacement, and the loaded
        # list is a single page, so every VPN beyond that page would be dropped
        # from the group on this branch — and merging the proposed change would
        # carry the removals to main, leaving those services with nothing to
        # regenerate them. The server emits the same GroupMemberAdded event
        # either way, so the trigger still fires.
        run_async(
            l3vpns_group.add_relationships(relation_to_update="members", related_nodes=[vpn.id])
        )

        # Wait for the L3VPN generator (fired by the group-membership trigger
        # above) to materialize the VRF / interfaces / IPs before triggering
        # artifact rendering, otherwise downstream artifacts render against
        # stale data and the proposed change shows no diff against main.
        def _is_active() -> bool:
            v = run_async(client.get(kind="ServiceL3Vpn", name__value=name))
            return v.status.value == "active"

        deadline = time.monotonic() + 120
        generated = _is_active()
        while not generated and time.monotonic() < deadline:
            time.sleep(2)
            generated = _is_active()
        if not generated:
            # The generator is no longer part of the proposed-change pipeline
            # (`execute_in_proposed_change: false`), so there is no second
            # chance: if the group trigger never fired, the branch holds only
            # the VPN rows and the proposed change will show no config diff.
            # Say so here rather than reporting success on an empty change.
            st.warning(
                "The L3VPN generator did not finish within 120s, so the VRF, "
                "PE-CE addressing and eBGP sessions may be missing and the "
                "proposed change may show no config diff. Check that "
                "`trigger-l3vpn-generator` exists (objects/events/00_triggers.yml "
                "is loaded by `invoke bootstrap`) and see the task-worker logs: "
                "`docker compose -p sp-demo logs task-worker --tail 200`."
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
