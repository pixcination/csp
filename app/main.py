"""
Streamlit entry point (`streamlit run app/main.py`, normally launched via
`python launch.py` from the project root). Defines page nav explicitly with
st.navigation/st.Page rather than relying on filename-based auto-discovery,
so page titles/icons/order are controlled here in one place.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

from app.theme import APP_NAME

PAGES_DIR = Path(__file__).resolve().parent / "pages"

st.set_page_config(
    page_title=APP_NAME,
    page_icon=":material/target:",
    layout="wide",
    initial_sidebar_state="expanded",
)

pages = [
    # Phase 14: the Screener is the landing page -- ask, run, pick a trade --
    # and carries Command Center's session banner on top. Trade Detail opens
    # from a selected row (?run=...&trade=...). Command Center stays for the
    # open-position decisions, account capacity and the rules in force.
    st.Page(str(PAGES_DIR / "8_Screener.py"), title="Screener",
            icon=":material/filter_alt:", default=True),
    st.Page(str(PAGES_DIR / "9_Trade_Detail.py"), title="Trade Detail",
            icon=":material/query_stats:"),
    st.Page(str(PAGES_DIR / "10_Strategies.py"), title="Strategies",
            icon=":material/category:"),
    # Phase 18: tracked forward tests and taken trades -- marks, attribution,
    # probabilities from now, hold vs managed outcomes.
    st.Page(str(PAGES_DIR / "11_Tracking.py"), title="Tracking",
            icon=":material/timeline:"),
    st.Page(str(PAGES_DIR / "0_Command_Center.py"), title="Command Center",
            icon=":material/bolt:"),
    st.Page(str(PAGES_DIR / "1_Decisions.py"), title="Decisions",
            icon=":material/fact_check:"),
    st.Page(str(PAGES_DIR / "2_Wheel.py"), title="Wheel",
            icon=":material/rotate_right:"),
    st.Page(str(PAGES_DIR / "3_Validation.py"), title="Validation",
            icon=":material/verified:"),
    st.Page(str(PAGES_DIR / "4_Portfolio.py"), title="Portfolio",
            icon=":material/donut_large:"),
    st.Page(str(PAGES_DIR / "5_Signals.py"), title="Signals",
            icon=":material/insights:"),
    st.Page(str(PAGES_DIR / "6_Universe.py"), title="Universe",
            icon=":material/list_alt:"),
    st.Page(str(PAGES_DIR / "7_Settings.py"), title="Settings",
            icon=":material/tune:"),
]
# The Phase-2 Scanner / Ticker Detail / Trade Log pages were retired to
# legacy/ in Phase 8; the Screener and Trade Detail pages (Phase 14) replace them.

with st.sidebar:
    st.markdown(f"## {APP_NAME}")
    st.caption("Wheel research and management -- no order placement")
    try:
        from core.market_calendar import classify
        _info = classify()
        _sev, _msg = _info.banner()
        _dot = {"ok": ":green[o]", "info": ":blue[o]"}.get(_sev, ":orange[o]")
        st.markdown(f"{_dot} **{_info.state.value.replace('_', ' ').title()}**")
        if not _info.is_open:
            st.caption(f"Marks {_info.staleness_label}")
    except Exception:
        pass
    st.divider()

nav = st.navigation(pages)
nav.run()
