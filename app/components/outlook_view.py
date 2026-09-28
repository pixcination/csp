"""
Outlook gauges (Phase 20) shared by the Universe and Trade Detail pages:
three dials for one symbol at one horizon, the line saying what moves
Direction, the implied-vs-historical downside sentence, and the honesty
caption (skill, sample, what the dials are not).
"""
from __future__ import annotations

import numpy as np
import streamlit as st

from analytics import outlook
from app.components.charts import outlook_gauge


def _num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def gauges(rec: dict | None, key: str) -> None:
    if not rec:
        st.caption("No Outlook for this symbol yet: it is built by the pipeline's data "
                   "stages (Command Center run, or the nightly job).")
        return
    cols = st.columns(3)
    for col, dial in zip(cols, outlook.DIALS):
        fig = outlook_gauge(dial, _num(rec.get(dial)), _num(rec.get(f"{dial}_lo")),
                            _num(rec.get(f"{dial}_hi")), _num(rec.get(f"{dial}_base")),
                            rec.get(f"{dial}_conf"))
        col.plotly_chart(fig, width="stretch", key=f"{key}|{dial}",
                         config={"displayModeBar": False})
    h = rec.get("horizon")
    lines = []
    skill = _num(rec.get("direction_skill"))
    raw = _num(rec.get("direction_raw"))
    lines.append(
        f"**Direction** P(up > ¼ EM) {rec.get('p_up', 0):.0%} vs P(down > ¼ EM) "
        f"{rec.get('p_down', 0):.0%}"
        + (f"; model reading {raw:.1f}" if raw is not None else "")
        + (f", walk-forward skill {skill:+.3f}" if skill is not None else "")
        + (" -- about zero, so the arrow sits at neutral" if rec.get("direction_conf") == "none"
           else ""))
    if rec.get("direction_why"):
        lines.append(f"What moves it: {rec['direction_why']}")
    lines.append(
        f"**Range** P(inside ±1 EM) {rec.get('p_inside', 0):.0%} vs its normal "
        f"{rec.get('base_inside', 0):.0%}"
        + (f" (EM from RV20 {rec.get('rv20', 0):.0%})" if rec.get("rv20") else "")
        + (f"; inside the IV move ±{rec['em_iv_pct']:.1%}: {rec['p_inside_iv_em']:.0%}"
           if _num(rec.get("p_inside_iv_em")) is not None else ""))
    if _num(rec.get("iv")) and _num(rec.get("forecast_rv")):
        lines.append(f"**Volatility** IV {rec['iv']:.0%} vs forecast realised "
                     f"{rec['forecast_rv']:.0%} (the engine's H/T paths); not walk-forward "
                     f"tested (needs IV history)")
    sentence = outlook.divergence_sentence(rec)
    if sentence:
        lines.append(sentence)
    st.markdown("  \n".join(lines))
    st.caption(f"At {h:.0f} calendar days. Band = uncertainty (wider where skill is low or the "
               "engine and the pooled model disagree); dotted tick = the stock's normal "
               "position; ●●● = confidence. Display only: the Outlook does not rank or gate "
               "trades.")
