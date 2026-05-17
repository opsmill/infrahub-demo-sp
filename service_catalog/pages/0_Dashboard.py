"""Dashboard: list existing L3VPNs and SD-WAN services in the selected branch."""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from utils import client_for, run_async

st.title("Service Catalog Dashboard")


@st.cache_data(ttl=10)
def _branches() -> list[str]:
    client = client_for()
    return list(run_async(client.branch.all()).keys())


branch = st.selectbox("Branch", options=_branches(), index=0)
client = client_for(branch=branch)

st.subheader("L3VPN services")
vpns = run_async(client.all(kind="ServiceL3Vpn", prefetch_relationships=True))
if not vpns:
    st.info("No L3VPNs yet. Use **Create L3VPN** to define your first service.")
else:
    rows = []
    for vpn in vpns:
        sites = run_async(
            client.filters(kind="ServiceL3VpnSite", l3vpn__ids=[vpn.id], branch=branch)
        )
        rd = vpn.vrf.peer.vrf_rd.value if vpn.vrf and vpn.vrf.peer else ""
        rows.append(
            {
                "name": vpn.name.value,
                "tenant": vpn.tenant.peer.name.value if vpn.tenant and vpn.tenant.peer else "",
                "vpn_id": vpn.vpn_id.value,
                "RD": rd,
                "# sites": len(sites),
                "status": vpn.status.value,
            }
        )

    df = pd.DataFrame(rows)
    st.metric("Active L3VPNs", len([r for r in rows if r["status"] == "active"]))
    st.metric("Total L3VPN sites", sum(r["# sites"] for r in rows))

    tenant_filter = st.text_input("Filter L3VPNs by tenant", key="l3vpn_tenant_filter")
    if tenant_filter:
        df = df[df["tenant"].str.contains(tenant_filter, case=False, na=False)]

    st.dataframe(df, use_container_width=True)

st.markdown("---")
st.subheader("SD-WAN services")
sdwans = run_async(client.all(kind="ServiceSdwan", prefetch_relationships=True))
if not sdwans:
    st.info("No SD-WAN services yet. Use **Create SD-WAN service** to define your first.")
else:
    sdwan_rows = []
    for svc in sdwans:
        sites = run_async(
            client.filters(kind="ServiceSdwanSite", sdwan__ids=[svc.id], branch=branch)
        )
        sdwan_rows.append(
            {
                "name": svc.name.value,
                "tenant": svc.tenant.peer.name.value if svc.tenant and svc.tenant.peer else "",
                "vendor": svc.vendor.value,
                "topology": svc.topology.value,
                "service_id": svc.service_id.value,
                "# sites": len(sites),
                "status": svc.status.value,
            }
        )
    sdwan_df = pd.DataFrame(sdwan_rows)
    st.metric("Active SD-WAN services", len([r for r in sdwan_rows if r["status"] == "active"]))
    st.metric("Total SD-WAN sites", sum(r["# sites"] for r in sdwan_rows))
    st.dataframe(sdwan_df, use_container_width=True)

st.caption(
    f"Open the [Infrahub UI]({os.environ.get('INFRAHUB_UI_URL', 'http://localhost:8000')}/)  "
    " to drill into any service.",
)
