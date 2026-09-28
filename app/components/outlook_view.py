"""
Outlook gauges (Phase 20) shared by the Universe and Trade Detail pages:
Direction and Range (vs the realised-vol move), Volatility (relative
richness across the universe) with the IV / forecast ratio on a log scale,
the line saying what moves Direction, the implied-vs-historical downside
sentence, and the honesty caption. A reading with no measurable skill is
greyed out as "no measurable edge" (Tom, 2026-09-28).
"""
from __future__ import annotations

import numpy as np
import streamlit as st

from analytics import outlook
from app.components.charts import NO_EDGE, outlook_gauge, vol_ratio_gauge


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
    top = float(outlook.cfg().get("vol_ratio_scale_max", 2.0))
    cols = st.columns(4)
    for col, dial in zip(cols, outlook.DIALS):
        fig = outlook_gauge(dial, _num(rec.get(dial)), _num(rec.get(f"{dial}_lo")),
                            _num(rec.get(f"{dial}_hi")), _num(rec.get(f"{dial}_base")),
                            rec.get(f"{dial}_conf"), no_edge=outlook.no_edge(rec, dial))
        col.plotly_chart(fig, width="stretch", key=f"{key}|{dial}",
                         config={"displayModeBar": False})
    cols[3].plotly_chart(vol_ratio_gauge(_num(rec.get("vol_ratio")), _num(rec.get("vol_ratio_lo")),
                                         _num(rec.get("vol_ratio_hi")), top),
                         width="stretch", key=f"{key}|ratio", config={"displayModeBar": False})
    h = rec.get("horizon")
    lines = []
    skill = _num(rec.get("direction_skill"))
    if outlook.no_edge(rec, "direction"):
        lines.append(f"**Direction (vs realised-vol move): {NO_EDGE}** -- walk-forward skill "
                     + (f"{skill:+.3f}" if skill is not None else "unknown")
                     + " for this symbol and horizon; the models' reading "
                       f"(P(up) {rec.get('p_up', 0):.0%} vs P(down) {rec.get('p_down', 0):.0%}) "
                       "is noise, so no dial is shown.")
    else:
        lines.append(
            f"**Direction (vs realised-vol move)** P(up > ¼ EM) {rec.get('p_up', 0):.0%} vs "
            f"P(down > ¼ EM) {rec.get('p_down', 0):.0%}"
            + (f", walk-forward skill {skill:+.3f}" if skill is not None else ""))
        if rec.get("direction_why"):
            lines.append(f"What moves it: {rec['direction_why']}")
    lines.append(
        f"**Range (vs realised-vol move)** P(inside ±1 EM) {rec.get('p_inside', 0):.0%} vs its "
        f"normal {rec.get('base_inside', 0):.0%}"
        + (f" (EM from RV20 {rec.get('rv20', 0):.0%})" if rec.get("rv20") else "")
        + (f" · **P(inside the IV move ±{rec['em_iv_pct']:.1%}): {rec['p_inside_iv_em']:.0%}**"
           if _num(rec.get("p_inside_iv_em")) is not None else ""))
    if _num(rec.get("vol_ratio")):
        lines.append(f"**Volatility (relative richness)** IV {rec['iv']:.0%} vs forecast realised "
                     f"{rec['forecast_rv']:.0%} = {rec['vol_ratio']:.2f}x; richer than "
                     f"{_num(rec.get('volatility')) * 10:.0f}% of the universe at this horizon. "
                     "Not walk-forward tested (needs IV history).")
    sentence = outlook.divergence_sentence(rec)
    if sentence:
        lines.append(sentence)
    st.markdown("  \n".join(lines))
    st.caption(f"At {h:.0f} calendar days. 'EM' is the move implied by 20-day realised vol, so "
               "these probabilities can be tested on 20+ years of prices; the IV move sits "
               "beside it. Band = uncertainty; dotted tick = the stock's normal position; "
               "●●● = confidence. Display only: the Outlook does not rank or gate trades.")
