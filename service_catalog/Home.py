"""Infrahub Service Catalog — Main Entry Point."""

from __future__ import annotations

import streamlit as st
from utils import display_logo

st.set_page_config(
    page_title="Infrahub SP Service Catalog",
    page_icon="🔗",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"Get Help": None, "Report a bug": None, "About": None},
)

display_logo()

dashboard = st.Page(
    "pages/0_Dashboard.py", title="Dashboard", icon="📊", default=True, url_path="dashboard"
)
create_l3vpn = st.Page("pages/1_Create_L3VPN.py", title="Create L3VPN", icon="🔗")
create_sdwan = st.Page("pages/2_Create_SDWAN.py", title="Create SD-WAN", icon="🛰️")
batfish_check = st.Page("pages/3_Batfish_Check.py", title="Batfish Check", icon="🧪")

pg = st.navigation(
    {
        "": [dashboard],
        "Service Catalog": [create_l3vpn, create_sdwan],
        "Validation": [batfish_check],
    }
)

pg.run()
