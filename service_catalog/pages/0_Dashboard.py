"""Dashboard: list existing L3VPNs in the selected branch."""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from utils import client_for, run_async

st.title("L3VPN Dashboard")


@st.cache_data(ttl=10)
def _branches() -> list[str]:
    client = client_for()
    return [b.name for b in run_async(client.branch.all())]


branch = st.selectbox("Branch", options=_branches(), index=0)
client = client_for(branch=branch)

vpns = run_async(client.all(kind="ServiceL3Vpn", prefetch_relationships=True))

if not vpns:
    st.info("No L3VPNs yet. Use **Create L3VPN** to define your first service.")
    st.stop()

rows = []
for vpn in vpns:
    sites = run_async(client.filters(kind="ServiceL3VpnSite", l3vpn__id=vpn.id, branch=branch))
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
st.metric("Total sites", sum(r["# sites"] for r in rows))

tenant_filter = st.text_input("Filter by tenant")
if tenant_filter:
    df = df[df["tenant"].str.contains(tenant_filter, case=False, na=False)]

st.dataframe(df, use_container_width=True)

st.caption(
    f"Open the [Infrahub UI]({os.environ.get('INFRAHUB_UI_URL', 'http://localhost:8000')}/)  "
    " to drill into any L3VPN.",
)
