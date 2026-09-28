"""
Shared Plotly chart builders. The first half was written for the (now
retired, see legacy/) single-leg Ticker Detail page; the Trade Detail builders
at the end (Phase 14) take any `Position` -- several strike lines, T+n payoff
curves, Greeks as small multiples, price x IV heatmap, chain OI with the legs
hatched. Every chart pulls
its colors from app/theme.py rather than Plotly defaults, and follows the
dataviz skill's mark specs (thin lines, direct labels over legends where a
chart has <=4 series, status colors reserved for pass/fail-type meaning).
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from app.theme import (BASELINE as BASELINE_INK, CATEGORICAL, CHART_SURFACE,
                       DIVERGING_MIDPOINT, DIVERGING_NEGATIVE, DIVERGING_POSITIVE, MUTED_INK,
                       PLOTLY_TEMPLATE, PRIMARY_INK, SECONDARY_INK, STATUS)
from analytics.options_math import bs_price_greeks, expected_move


def _apply_layout(fig: go.Figure, title: str = "", height: int = 420) -> go.Figure:
    fig.update_layout(**PLOTLY_TEMPLATE["layout"])
    fig.update_layout(title=title, height=height, margin=dict(l=50, r=30, t=50, b=40),
                       hovermode="x unified")
    return fig


def price_vol_chart(daily: pd.DataFrame, strike: float | None = None,
                     expiration: pd.Timestamp | None = None,
                     bb_window: int = 20, bb_k: float = 2.0,
                     lookback_days: int = 180) -> go.Figure:
    """Price history with a Bollinger-style volatility envelope
    (SMA +/- k*rolling std), the selected strike marked as a horizontal
    line and the selected expiration as a vertical line."""
    d = daily.tail(lookback_days + bb_window).copy()
    sma = d["close"].rolling(bb_window).mean()
    std = d["close"].rolling(bb_window).std()
    d = d.tail(lookback_days)
    sma, std = sma.tail(lookback_days), std.tail(lookback_days)
    upper, lower = sma + bb_k * std, sma - bb_k * std

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=d["date"], y=upper, line=dict(width=0),
                              showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=d["date"], y=lower, line=dict(width=0), fill="tonexty",
                              fillcolor="rgba(57,135,229,0.10)", name=f"±{bb_k:g}σ band",
                              hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=d["date"], y=d["close"], line=dict(color=CATEGORICAL[0], width=2),
                              name="Close"))
    fig.add_trace(go.Scatter(x=d["date"], y=sma, line=dict(color=MUTED_INK, width=1, dash="dot"),
                              name=f"SMA{bb_window}"))

    if strike is not None:
        fig.add_hline(y=strike, line=dict(color=STATUS["warning"], width=1.5, dash="dash"),
                       annotation_text=f"Strike ${strike:g}", annotation_position="top left")
    if expiration is not None:
        # add_vline's annotation-placement math breaks on datetime x-axes
        # (tries to sum() the x-values internally) -- add the line and its
        # label as separate shape/annotation calls instead, which sidesteps
        # that code path entirely.
        exp_dt = pd.Timestamp(expiration).to_pydatetime()
        fig.add_shape(type="line", x0=exp_dt, x1=exp_dt, y0=0, y1=1, yref="paper",
                       line=dict(color=MUTED_INK, width=1, dash="dot"))
        fig.add_annotation(x=exp_dt, y=1, yref="paper", yanchor="bottom",
                            text="Expiration", showarrow=False, font=dict(color=MUTED_INK))

    return _apply_layout(fig, "Price & volatility band")


def iv_vs_rv_chart(iv_history: pd.DataFrame, rv_series: pd.Series | None) -> go.Figure:
    fig = go.Figure()
    if not iv_history.empty:
        fig.add_trace(go.Scatter(x=iv_history["date"], y=iv_history["iv"] * 100,
                                  mode="lines+markers", line=dict(color=CATEGORICAL[2], width=2),
                                  marker=dict(size=7), name="IV (accumulated snapshots)"))
    if rv_series is not None and not rv_series.empty:
        fig.add_trace(go.Scatter(x=rv_series.index, y=rv_series.values * 100,
                                  line=dict(color=CATEGORICAL[3], width=2), name="Realized vol"))
    if iv_history.empty and (rv_series is None or rv_series.empty):
        fig.add_annotation(text="No data yet", showarrow=False,
                            font=dict(color=MUTED_INK))
    fig.update_layout(yaxis_title="Annualized vol (%)")
    return _apply_layout(fig, "IV vs. realized vol")


def pnl_at_expiration_chart(strike: float, premium: float, commission: float = 0.0,
                             spot: float | None = None) -> go.Figure:
    """Standard short-put payoff at expiration: flat max profit above the
    strike, 1:1 loss below it. Contract = 100 shares."""
    net_credit = premium * 100 - commission
    breakeven = strike - premium + commission / 100.0
    lo, hi = strike * 0.75, strike * 1.15
    prices = np.linspace(lo, hi, 200)
    pnl = np.where(prices >= strike, net_credit,
                    net_credit - (strike - prices) * 100)

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=prices, y=pnl, mode="lines", line=dict(color=CATEGORICAL[0], width=2),
                              fill="tozeroy",
                              fillcolor="rgba(25,158,112,0.12)", name="P&L at expiration"))
    fig.add_hline(y=0, line=dict(color=MUTED_INK, width=1))
    fig.add_vline(x=breakeven, line=dict(color=STATUS["warning"], width=1.5, dash="dash"),
                  annotation_text=f"Breakeven ${breakeven:,.2f}", annotation_position="bottom right")
    fig.add_vline(x=strike, line=dict(color=MUTED_INK, width=1, dash="dot"),
                  annotation_text=f"Strike ${strike:g}", annotation_position="top left")
    if spot is not None:
        fig.add_vline(x=spot, line=dict(color=CATEGORICAL[1], width=1.5),
                      annotation_text=f"Spot ${spot:,.2f}", annotation_position="top right")

    fig.update_layout(xaxis_title="Underlying price at expiration", yaxis_title="P&L ($)")
    return _apply_layout(fig, "P&L at expiration")


def pnl_over_time_chart(spot: float, strike: float, premium: float, dte_days: int,
                         vol: float, rate: float, commission: float = 0.0) -> go.Figure:
    """P&L path day-by-day under three price scenarios (flat / +1sigma /
    -1sigma at expiration, Brownian-scaled for intermediate days) -- shows
    theta decay accrual, the actual shape of a CSP's return path."""
    net_credit_per_share = premium
    days = np.arange(0, dte_days + 1)
    sigma_1_at_expiry = expected_move(spot, dte_days, vol, sigmas=1.0)

    scenarios = {
        "Flat": np.zeros_like(days, dtype=float),
        "+1σ move": sigma_1_at_expiry * np.sqrt(days / max(dte_days, 1)),
        "-1σ move": -sigma_1_at_expiry * np.sqrt(days / max(dte_days, 1)),
    }
    colors = [CATEGORICAL[0], STATUS["good"], STATUS["critical"]]

    fig = go.Figure()
    for (label, price_shift), color in zip(scenarios.items(), colors):
        scenario_spot = spot + price_shift
        remaining_dte = dte_days - days
        values = [bs_price_greeks(s, strike, max(rd, 0), vol, rate, "put").price
                  for s, rd in zip(scenario_spot, remaining_dte)]
        pnl = (net_credit_per_share - np.array(values)) * 100 - commission
        fig.add_trace(go.Scatter(x=days, y=pnl, mode="lines", line=dict(color=color, width=2),
                                  name=label))

    fig.add_hline(y=0, line=dict(color=MUTED_INK, width=1))
    fig.update_layout(xaxis_title="Days since entry", yaxis_title="P&L ($)")
    return _apply_layout(fig, "P&L over time (theta decay by scenario)")


def greeks_over_time_chart(spot: float, strike: float, dte_days: int, vol: float,
                            rate: float) -> go.Figure:
    """Net (short-put) greeks across the DTE window, spot/vol held constant
    -- isolates how theta/gamma risk shifts purely from time decay."""
    days = np.arange(0, dte_days + 1)
    remaining = dte_days - days
    rows = [bs_price_greeks(spot, strike, rd, vol, rate, "put") for rd in remaining]

    fig = go.Figure()
    specs = [("delta", "Delta", CATEGORICAL[0]), ("theta", "Theta", CATEGORICAL[4]),
             ("gamma", "Gamma", CATEGORICAL[6]), ("vega", "Vega", CATEGORICAL[7])]
    for attr, label, color in specs:
        # Short-put position: negate the long-put greek to show the seller's exposure.
        y = [-getattr(g, attr) for g in rows]
        fig.add_trace(go.Scatter(x=days, y=y, mode="lines", line=dict(color=color, width=2), name=label))

    fig.add_hline(y=0, line=dict(color=MUTED_INK, width=1))
    fig.update_layout(xaxis_title="Days since entry", yaxis_title="Greek value (short-put position)")
    return _apply_layout(fig, "Net greeks across the DTE window", height=380)


def probability_cone_chart(daily: pd.DataFrame, dte_days: int, vol: float,
                            history_days: int = 30) -> go.Figure:
    """Recent price history plus a forward-looking 1σ/2σ expected-move cone
    from today out to expiration."""
    hist = daily.tail(history_days)
    spot = float(daily["close"].iloc[-1])
    today = daily["date"].iloc[-1]

    horizon = np.arange(0, dte_days + 1)
    future_dates = [today + pd.Timedelta(days=int(h)) for h in horizon]
    sigma1 = np.array([expected_move(spot, h, vol, 1.0) for h in horizon])
    sigma2 = np.array([expected_move(spot, h, vol, 2.0) for h in horizon])

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=hist["date"], y=hist["close"], line=dict(color=CATEGORICAL[0], width=2),
                              name="Recent price"))

    fig.add_trace(go.Scatter(x=future_dates, y=spot + sigma2, line=dict(width=0),
                              showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=future_dates, y=spot - sigma2, line=dict(width=0), fill="tonexty",
                              fillcolor="rgba(57,135,229,0.08)", name="±2σ", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=future_dates, y=spot + sigma1, line=dict(width=0),
                              showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=future_dates, y=spot - sigma1, line=dict(width=0), fill="tonexty",
                              fillcolor="rgba(57,135,229,0.16)", name="±1σ", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=future_dates, y=[spot] * len(future_dates),
                              line=dict(color=MUTED_INK, width=1, dash="dot"), name="Spot (flat)"))

    return _apply_layout(fig, "Probability cone (expected move to expiration)")


def chain_volume_oi_chart(chain: pd.DataFrame, expiration) -> go.Figure:
    """Volume + OI overlay across strikes for a given expiration, puts and
    calls as separate bar traces so the most active strikes are visually
    obvious rather than buried in a table."""
    exp_chain = chain[pd.to_datetime(chain["expiration"]) == pd.to_datetime(expiration)].sort_values("strike_price")
    if exp_chain.empty:
        fig = go.Figure()
        fig.add_annotation(text="No chain data for this expiration", showarrow=False, font=dict(color=MUTED_INK))
        return _apply_layout(fig, "Volume / Open interest by strike", height=320)

    fig = go.Figure()
    fig.add_trace(go.Bar(x=exp_chain["strike_price"], y=exp_chain["put_open_interest"],
                          name="Put OI", marker_color=CATEGORICAL[5], opacity=0.85))
    fig.add_trace(go.Bar(x=exp_chain["strike_price"], y=exp_chain["call_open_interest"],
                          name="Call OI", marker_color=CATEGORICAL[0], opacity=0.85))
    fig.update_layout(barmode="group", xaxis_title="Strike", yaxis_title="Open interest")
    return _apply_layout(fig, "Open interest by strike", height=320)


def levels_chart(frame: pd.DataFrame, levels: list[tuple[str, str]],
                 tests: pd.DataFrame | None = None, test_label: str = "",
                 years: float = 2.0) -> go.Figure:
    """Price with moving-average levels and the tests of one level (Phase 10).

    One y-axis. Price is categorical 1; up to three levels take categorical
    2, 3 and 5 in fixed order and are direct-labelled at the right edge. Test markers use the
    reserved status colours with distinct symbols (held = circle, broke = x)
    and a legend, so the outcome is never colour alone.
    """
    cutoff = pd.to_datetime(frame["date"]).max() - pd.DateOffset(years=years)
    view = frame[pd.to_datetime(frame["date"]) >= cutoff]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=view["date"], y=view["close"], name="Close",
                             line=dict(color=CATEGORICAL[0], width=2),
                             hovertemplate="%{y:,.2f}"))
    # Level hues skip categorical green (4) and red (6): the test markers use
    # the reserved status good/critical colours, and a green level line next
    # to green "held" markers would make identity ambiguous.
    level_hues = [CATEGORICAL[1], CATEGORICAL[2], CATEGORICAL[4]]
    labels = []
    for i, (label, column) in enumerate(levels[:3]):
        if column not in view:
            continue
        color = level_hues[i]
        fig.add_trace(go.Scatter(x=view["date"], y=view[column], name=label,
                                 line=dict(color=color, width=2),
                                 hovertemplate="%{y:,.2f}"))
        last = view[column].dropna()
        if not last.empty:
            labels.append([float(last.iloc[-1]), label])
    # Right-edge direct labels, pushed apart where the levels converge.
    if labels:
        values = pd.concat([view["close"]] + [view[c] for _, c in levels[:3] if c in view])
        gap = 0.045 * float(values.max() - values.min() or 1.0)
        labels.sort()
        for j in range(1, len(labels)):
            labels[j][0] = max(labels[j][0], labels[j - 1][0] + gap)
        for y, label in labels:
            fig.add_annotation(x=view["date"].iloc[-1], y=y, text=label, showarrow=False,
                               xanchor="left", xshift=6, font=dict(color=MUTED_INK, size=11))
    if tests is not None and not tests.empty:
        shown = tests[pd.to_datetime(tests["date"]) >= cutoff]
        for held, name, color, symbol in ((True, "held", STATUS["good"], "circle"),
                                          (False, "broke", STATUS["critical"], "x")):
            part = shown[shown["held"] == held]
            if part.empty:
                continue
            fig.add_trace(go.Scatter(
                x=part["date"], y=part["level"], mode="markers",
                name=f"{test_label} test: {name}",
                marker=dict(color=color, size=10, symbol=symbol,
                            line=dict(color=CHART_SURFACE, width=2)),
                hovertemplate=f"{name}, pierce %{{customdata:.2f}} ATR<extra></extra>",
                customdata=part["pierce_atr"]))
    fig = _apply_layout(fig, height=460)
    fig.update_layout(legend=dict(orientation="h", y=1.08, x=0),
                      margin=dict(l=50, r=90, t=40, b=40))
    return fig


# --- Trade Detail builders (Phase 14): any Position, any number of legs ------------

# Leg identity takes categorical red (short: the downside leg) and violet
# (long); status colours stay reserved for state. MA overlays on the price
# chart therefore use aqua, yellow and magenta only.
LEG_COLORS = {"short": CATEGORICAL[5], "long": CATEGORICAL[4]}
MA_HUES = [CATEGORICAL[1], CATEGORICAL[2], CATEGORICAL[6]]
MODEL_COLORS = {"G": CATEGORICAL[0], "H": CATEGORICAL[1], "T": CATEGORICAL[2],
                "blend": CATEGORICAL[5]}


def _style_axes(fig: go.Figure) -> go.Figure:
    """The template styles only the first axis pair; subplots need every axis."""
    axis = PLOTLY_TEMPLATE["layout"]["xaxis"]
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    return fig


def _money(v: float) -> str:
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _vline(fig: go.Figure, x, label: str, color: str, dash: str = "dot",
           position: str = "top") -> None:
    """Vertical line plus label as separate shape/annotation (add_vline's
    annotation maths fails on datetime axes -- see price_vol_chart)."""
    x_val = pd.Timestamp(x).to_pydatetime()
    fig.add_shape(type="line", x0=x_val, x1=x_val, y0=0, y1=1, yref="paper",
                  line=dict(color=color, width=1, dash=dash))
    if label:
        fig.add_annotation(x=x_val, y=1 if position == "top" else 0, yref="paper",
                           yanchor="bottom" if position == "top" else "top",
                           text=label, showarrow=False, font=dict(color=MUTED_INK, size=10))


def trade_price_chart(frame: pd.DataFrame, ma_columns: list[tuple[str, str]],
                      strikes: list[tuple[str, float, str]],
                      levels: pd.DataFrame | None = None,
                      cone: pd.DataFrame | None = None,
                      events: list[tuple] | None = None,
                      lookback: int = 180, title: str = "") -> go.Figure:
    """Candles with MA overlays, respected levels, the trade's strike lines,
    the IV and straddle expected-move cones to expiry and event markers.

    `strikes` = [(label, price, "short"|"long")]; `levels` = support table rows
    (level_id, level, strong); `cone` from trade_detail.em_cone;
    `events` = [(date, label)].
    """
    view = frame.tail(lookback)
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=view["date"], open=view["open"], high=view["high"], low=view["low"],
        close=view["close"], name="Price",
        increasing=dict(line=dict(color=CATEGORICAL[0], width=1), fillcolor=CATEGORICAL[0]),
        decreasing=dict(line=dict(color=MUTED_INK, width=1), fillcolor=MUTED_INK)))
    for i, (label, column) in enumerate(ma_columns[:3]):
        if column in view:
            fig.add_trace(go.Scatter(x=view["date"], y=view[column], name=label,
                                     line=dict(color=MA_HUES[i], width=1.5),
                                     hovertemplate="%{y:,.2f}"))
    if cone is not None and not cone.empty:
        for name, color, dash in (("iv", CATEGORICAL[7], "dash"),
                                  ("straddle", CATEGORICAL[3], "dot")):
            if f"{name}_upper" not in cone:
                continue
            for side in ("upper", "lower"):
                fig.add_trace(go.Scatter(
                    x=cone["date"], y=cone[f"{name}_{side}"],
                    name=f"EM cone ({name})", legendgroup=name, showlegend=side == "upper",
                    line=dict(color=color, width=1.5, dash=dash),
                    hovertemplate=f"{name} EM {side}: %{{y:,.2f}}<extra></extra>"))
    if levels is not None and not levels.empty:
        for _, lv in levels.iterrows():
            strong = bool(lv.get("strong"))
            fig.add_hline(y=float(lv["level"]),
                          line=dict(color=STATUS["good"] if strong else BASELINE_INK,
                                    width=1.5 if strong else 1,
                                    dash="solid" if strong else "dot"),
                          annotation_text=f"{lv['level_id']}{' (strong)' if strong else ''}",
                          annotation_position="bottom left",
                          annotation_font=dict(color=MUTED_INK, size=10))
    for label, price, leg in strikes:
        fig.add_hline(y=price, line=dict(color=LEG_COLORS.get(leg, STATUS["warning"]),
                                         width=2, dash="dash"),
                      annotation_text=label, annotation_position="top right",
                      annotation_font=dict(color=SECONDARY_INK, size=11))
    for when, label in events or []:
        when = pd.Timestamp(when)
        if when >= pd.Timestamp(view["date"].iloc[0]):
            _vline(fig, when, label, MUTED_INK)
    if cone is not None and not cone.empty:
        _vline(fig, cone["date"].iloc[-1], "Expiry", SECONDARY_INK, dash="dash",
               position="bottom")
    fig.update_layout(xaxis_rangeslider_visible=False, yaxis_title="Price")
    fig = _apply_layout(fig, title, height=540)
    fig.update_layout(legend=dict(orientation="h", y=-0.08, x=0), hovermode="x",
                      margin=dict(l=50, r=30, t=40, b=60))
    return fig


def terminal_distribution_chart(dists: dict, strikes: list[tuple[str, float, str]],
                                spot: float, em: float | None = None) -> go.Figure:
    """Empirical vs lognormal terminal price densities (one axis, two series
    with a legend), strikes and +/-1 EM marked."""
    fig = go.Figure()
    series = [("empirical", CATEGORICAL[0]), ("lognormal", CATEGORICAL[2])]
    present = [dists[m] for m, _ in series if m in dists]
    values = np.concatenate(present) if present else np.array([spot * 0.9, spot * 1.1])
    lo, hi = np.percentile(values, [0.5, 99.5])
    bins = np.linspace(lo, hi, 90)
    centers = (bins[:-1] + bins[1:]) / 2
    for model, color in series:
        if model not in dists:
            continue
        density, _ = np.histogram(dists[model], bins=bins, density=True)
        fig.add_trace(go.Scatter(x=centers, y=density, mode="lines",
                                 name=dists.get("label", {}).get(model, model),
                                 line=dict(color=color, width=2),
                                 hovertemplate="$%{x:,.2f}: %{y:.4f}<extra>" + model + "</extra>"))
    for label, price, leg in strikes:
        fig.add_vline(x=price, line=dict(color=LEG_COLORS.get(leg, STATUS["warning"]),
                                         width=2, dash="dash"),
                      annotation_text=label,
                      annotation_position="top left" if leg == "long" else "top right",
                      annotation_font=dict(color=SECONDARY_INK, size=11))
    fig.add_vline(x=spot, line=dict(color=SECONDARY_INK, width=1),
                  annotation_text=f"Spot ${spot:,.2f}", annotation_position="top right",
                  annotation_font=dict(color=SECONDARY_INK, size=11))
    if em:
        for k in (-1, 1):
            fig.add_vline(x=spot + k * em, line=dict(color=MUTED_INK, width=1, dash="dot"),
                          annotation_text=f"{k:+d} EM", annotation_position="bottom right",
                          annotation_font=dict(color=MUTED_INK, size=10))
    fig.update_layout(xaxis_title="Price at expiry", yaxis_title="Density")
    fig = _apply_layout(fig, height=400)
    fig.update_layout(legend=dict(orientation="h", y=1.1, x=0), hovermode="closest")
    return fig


def payoff_chart(frame: pd.DataFrame, spot: float, breakevens: list[float],
                 strikes: list[tuple[str, float, str]]) -> go.Figure:
    """P&L at expiry and at T+n for any number of legs (from
    trade_detail.payoff_frame)."""
    fig = go.Figure()
    curves = list(dict.fromkeys(frame["curve"]))
    hues = [CATEGORICAL[0], CATEGORICAL[1], CATEGORICAL[2], CATEGORICAL[4]]
    for i, curve in enumerate(curves):
        part = frame[frame["curve"] == curve]
        fig.add_trace(go.Scatter(
            x=part["price"], y=part["pnl"], mode="lines", name=curve,
            line=dict(color=hues[i % 4], width=2.5 if curve == "expiry" else 1.5,
                      dash="solid" if curve == "expiry" else "dash"),
            hovertemplate="$%{x:,.2f}: $%{y:,.0f}<extra>" + curve + "</extra>"))
    fig.add_hline(y=0, line=dict(color=BASELINE_INK, width=1))
    for b in breakevens:
        fig.add_vline(x=b, line=dict(color=STATUS["serious"], width=1.5, dash="dot"),
                      annotation_text=f"Breakeven ${b:,.2f}", annotation_position="bottom left",
                      annotation_font=dict(color=SECONDARY_INK, size=10))
    for label, price, leg in strikes:
        fig.add_vline(x=price, line=dict(color=LEG_COLORS.get(leg, STATUS["warning"]),
                                         width=1, dash="dash"),
                      annotation_text=label,
                      annotation_position="top left" if leg == "long" else "top right",
                      annotation_font=dict(color=MUTED_INK, size=10))
    fig.add_vline(x=spot, line=dict(color=SECONDARY_INK, width=1),
                  annotation_text=f"Spot ${spot:,.2f}", annotation_position="top right",
                  annotation_font=dict(color=SECONDARY_INK, size=11))
    fig.update_layout(xaxis_title="Underlying price", yaxis_title="P&L ($, before fees)")
    fig = _apply_layout(fig, height=420)
    fig.update_layout(legend=dict(orientation="h", y=1.1, x=0), hovermode="x unified")
    return fig


def prob_curves_chart(curves: pd.DataFrame) -> go.Figure:
    """P(reach X% of max profit by day d) -- one small multiple per target,
    one line per model, shared 0-1 y scale."""
    from plotly.subplots import make_subplots
    # "expire worthless" is only reachable at expiry: a step, not a curve
    targets = sorted(int(t) for t in curves["target"].unique() if int(t) < 100)
    fig = make_subplots(rows=1, cols=len(targets), shared_yaxes=True,
                        subplot_titles=[("expire worthless" if t == 100 else f"reach {t}%")
                                        for t in targets])
    for j, target in enumerate(targets, start=1):
        part = curves[curves["target"] == target]
        for model in ("G", "H", "T"):
            m = part[part["model"] == model].sort_values("day")
            if m.empty:
                continue
            fig.add_trace(go.Scatter(
                x=m["day"], y=m["prob"], mode="lines", name=model, legendgroup=model,
                showlegend=j == 1, line=dict(color=MODEL_COLORS[model], width=2),
                hovertemplate="day %{x:.0f}: %{y:.0%}<extra>" + model + "</extra>"),
                row=1, col=j)
    fig = _style_axes(_apply_layout(fig, height=340))
    fig.update_yaxes(range=[0, 1], tickformat=".0%")
    fig.update_xaxes(title_text="calendar days")
    fig.update_layout(legend=dict(orientation="h", y=-0.3, x=0), hovermode="closest",
                      margin=dict(l=50, r=30, t=40, b=80))
    return fig


def greeks_time_chart(frame: pd.DataFrame) -> go.Figure:
    """Net position Greeks from entry to expiry, spot and IV held -- small
    multiples, since each Greek has its own scale (never a dual axis)."""
    from plotly.subplots import make_subplots
    specs = [("delta", "Delta (shares)", CATEGORICAL[0]),
             ("theta", "Theta ($/day)", CATEGORICAL[4]),
             ("gamma", "Gamma", CATEGORICAL[6]), ("vega", "Vega ($/vol pt)", CATEGORICAL[7])]
    fig = make_subplots(rows=1, cols=4, subplot_titles=[s[1] for s in specs])
    for j, (column, label, color) in enumerate(specs, start=1):
        fig.add_trace(go.Scatter(x=frame["day"], y=frame[column], mode="lines", name=label,
                                 line=dict(color=color, width=2), showlegend=False,
                                 hovertemplate="day %{x}: %{y:,.2f}<extra>" + column
                                               + "</extra>"),
                      row=1, col=j)
    fig = _style_axes(_apply_layout(fig, height=300))
    fig.update_xaxes(title_text="days after entry")
    return fig


def scenario_heatmap(grid: pd.DataFrame) -> go.Figure:
    """P&L over spot move x IV shift. Diverging: red loss, blue gain, gray 0,
    every cell labelled with its value."""
    table = grid.pivot_table(index="iv_shift", columns="move", values="pnl")
    bound = float(np.nanmax(np.abs(table.values))) or 1.0
    fig = go.Figure(go.Heatmap(
        z=table.values, x=[f"{m:+.0%}" for m in table.columns],
        y=[f"{s * 100:+.0f} vol" for s in table.index],
        zmin=-bound, zmax=bound, zmid=0,
        colorscale=[[0, DIVERGING_NEGATIVE], [0.5, DIVERGING_MIDPOINT],
                    [1, DIVERGING_POSITIVE]],
        text=[[_money(v) for v in row] for row in table.values], texttemplate="%{text}",
        textfont=dict(size=10, color=PRIMARY_INK),
        hovertemplate="spot %{x}, IV %{y}: %{text}<extra></extra>",
        colorbar=dict(title="P&L $")))
    fig.update_layout(xaxis_title="Spot move", yaxis_title="IV shift",
                      xaxis_type="category", yaxis_type="category")
    fig = _apply_layout(fig, height=360)
    fig.update_layout(hovermode="closest")
    return fig


def chain_liquidity_chart(window: pd.DataFrame) -> go.Figure:
    """Put open interest by strike; the trade's legs highlighted by colour
    AND hatching, so identity is never colour alone."""
    fig = go.Figure()
    colors = [LEG_COLORS.get(leg, CATEGORICAL[0]) for leg in window["leg"]]
    patterns = ["/" if leg else "" for leg in window["leg"]]
    fig.add_trace(go.Bar(
        x=window["strike"], y=window["open_interest"], name="Put OI",
        marker=dict(color=colors, pattern_shape=patterns,
                    line=dict(color=CHART_SURFACE, width=2)),
        customdata=np.stack([window["volume"].fillna(0).to_numpy(float),
                             window["width"].to_numpy(float)], axis=1),
        hovertemplate="$%{x:g}: OI %{y:,.0f}, volume %{customdata[0]:,.0f}, "
                      "bid/ask width $%{customdata[1]:.2f}<extra></extra>"))
    fig.update_layout(xaxis_title="Strike (hatched = the trade's legs)",
                      yaxis_title="Open interest", showlegend=False)
    fig = _apply_layout(fig, height=320)
    fig.update_layout(hovermode="closest")
    return fig


# --- Outlook (Phase 20) ---------------------------------------------------------

OUTLOOK_POLES = {"direction": ("bearish", "neutral", "bullish"),
                 "range": ("breakout", "its normal", "range-bound"),
                 "volatility": ("least rich", "median", "richest")}
OUTLOOK_LABELS = {"direction": "Direction (vs realised-vol move)",
                  "range": "Range (vs realised-vol move)",
                  "volatility": "Volatility (relative richness)"}
CONFIDENCE_DOTS = {"none": "○○○", "low": "●○○", "medium": "●●○", "high": "●●●"}
NO_EDGE = "no measurable edge"


def outlook_heatmap(table: pd.DataFrame, dial: str, value_column: str | None = None) -> go.Figure:
    """Symbols x horizons for one dial. Diverging around 5 (red below, gray
    at 5, blue above); each cell shows the score and confidence dots
    (○○○ none ... ●●● high). A Direction or Range reading with no
    measurable skill is drawn greyed out as 'no edge' instead of a 5
    (unless `value_column` asks for the raw model reading)."""
    column = value_column or dial
    frame = table.dropna(subset=[column])
    grid = frame.pivot_table(index="ticker", columns="horizon", values=column)
    conf = frame.pivot_table(index="ticker", columns="horizon", values=f"{dial}_conf",
                             aggfunc="first").reindex_like(grid)
    grid = grid.sort_index(ascending=False)
    conf = conf.reindex(grid.index)
    greyed = (conf == "none").to_numpy() if dial != "volatility" and column == dial \
        else np.zeros(grid.shape, bool)
    values = grid.to_numpy(float)
    shown = np.where(greyed, np.nan, values)
    text = [[("" if greyed[i][j] or not np.isfinite(v)
              else f"{v:.1f} {CONFIDENCE_DOTS.get(c, '')}")
             for j, (v, c) in enumerate(zip(row, crow))]
            for i, (row, crow) in enumerate(zip(values, conf.to_numpy()))]
    low, mid, high = OUTLOOK_POLES[dial]
    x = [f"{h}d" for h in grid.columns]
    y = list(grid.index)
    fig = go.Figure(go.Heatmap(
        z=shown, x=x, y=y, zmin=0, zmax=10, zmid=5,
        colorscale=[[0, DIVERGING_NEGATIVE], [0.5, DIVERGING_MIDPOINT], [1, DIVERGING_POSITIVE]],
        text=text, texttemplate="%{text}", textfont=dict(size=10, color=PRIMARY_INK),
        xgap=2, ygap=2, hoverongaps=False,
        hovertemplate="%{y} @ %{x}: %{z:.2f}<extra></extra>",
        colorbar=dict(title=OUTLOOK_LABELS[dial].split(" (")[0], tickvals=[0, 5, 10],
                      ticktext=[low, mid, high])))
    if greyed.any():
        fig.add_trace(go.Heatmap(
            z=np.where(greyed, 1.0, np.nan), x=x, y=y, zmin=0, zmax=1, showscale=False,
            colorscale=[[0, BASELINE_INK], [1, BASELINE_INK]], xgap=2, ygap=2,
            hoverongaps=False, text=np.where(greyed, "no edge", ""),
            texttemplate="%{text}", textfont=dict(size=9, color=MUTED_INK),
            hovertemplate=f"%{{y}} @ %{{x}}: {NO_EDGE} (walk-forward skill ~0)<extra></extra>"))
    fig.update_layout(xaxis_title="Horizon (calendar days)", xaxis_type="category",
                      yaxis_type="category")
    fig = _apply_layout(fig, height=max(360, 18 * len(grid) + 120))
    fig.update_layout(hovermode="closest", xaxis_side="top")
    return fig


def outlook_gauge(dial: str, score: float | None, lo: float | None, hi: float | None,
                  base: float | None, confidence: str | None = None,
                  no_edge: bool = False) -> go.Figure:
    """One dial as a horizontal line 0-10: the shaded band (uncertainty), the
    arrow at the score, a tick at the stock's normal position, pole labels.
    With `no_edge` the whole gauge is greyed out and says so."""
    low, mid, high = OUTLOOK_POLES[dial]
    fig = go.Figure()
    fig.add_shape(type="line", x0=0, x1=10, y0=0, y1=0, line=dict(color=BASELINE_INK, width=2))
    for x in (0, 5, 10):
        fig.add_shape(type="line", x0=x, x1=x, y0=-0.12, y1=0.12,
                      line=dict(color=MUTED_INK, width=1))
    if not no_edge and lo is not None and hi is not None and np.isfinite(lo) and np.isfinite(hi):
        fig.add_shape(type="rect", x0=lo, x1=hi, y0=-0.3, y1=0.3, line_width=0,
                      fillcolor="rgba(195,194,183,0.18)")
    if not no_edge and base is not None and np.isfinite(base):
        fig.add_shape(type="line", x0=base, x1=base, y0=-0.42, y1=0.42,
                      line=dict(color=SECONDARY_INK, width=2, dash="dot"))
    if no_edge:
        fig.add_trace(go.Scatter(
            x=[5], y=[0.55], mode="text", text=[NO_EDGE], textposition="middle center",
            textfont=dict(color=MUTED_INK, size=12),
            hovertemplate="Walk-forward skill is ~0 for this symbol and horizon: the models' "
                          "reading is noise, so none is shown.<extra></extra>"))
    elif score is not None and np.isfinite(score):
        color = (DIVERGING_POSITIVE if score > 5.25 else DIVERGING_NEGATIVE if score < 4.75
                 else SECONDARY_INK)
        fig.add_trace(go.Scatter(
            x=[score], y=[0.55], mode="markers+text", text=[f"{score:.1f}"],
            textposition="top center", textfont=dict(color=PRIMARY_INK, size=13),
            marker=dict(symbol="triangle-down", size=16, color=color,
                        line=dict(color=CHART_SURFACE, width=2)),
            hovertemplate=f"{dial} %{{x:.2f}}" + (f" (band {lo:.1f}-{hi:.1f})"
                                                  if lo is not None and hi is not None else "")
            + "<extra></extra>"))
    title = OUTLOOK_LABELS[dial] + ("" if no_edge or not confidence else
                                    f"  {CONFIDENCE_DOTS.get(confidence, '')} {confidence}")
    fig.update_layout(**PLOTLY_TEMPLATE["layout"])
    fig.update_layout(
        height=120, margin=dict(l=10, r=10, t=28, b=8), showlegend=False,
        title=dict(text=title, font=dict(size=13, color=MUTED_INK if no_edge else SECONDARY_INK),
                   x=0.01),
        xaxis=dict(range=[-0.4, 10.4], tickvals=[0, 5, 10], ticktext=[low, mid, high],
                   showgrid=False, zeroline=False, tickfont=dict(color=MUTED_INK, size=11)),
        yaxis=dict(range=[-0.6, 1.2], visible=False))
    return fig


def vol_ratio_gauge(ratio: float | None, lo: float | None = None, hi: float | None = None,
                    top: float = 2.0) -> go.Figure:
    """IV / forecast realised vol as a number on a log scale 1/top .. top
    (1 = fair); values beyond the scale sit at its end, labelled."""
    import math
    fig = go.Figure()
    lo_x, hi_x = math.log10(1.0 / top), math.log10(top)
    fig.add_shape(type="line", x0=lo_x, x1=hi_x, y0=0, y1=0,
                  line=dict(color=BASELINE_INK, width=2))
    for tick in (1.0 / top, 1.0, top):
        fig.add_shape(type="line", x0=math.log10(tick), x1=math.log10(tick), y0=-0.12, y1=0.12,
                      line=dict(color=MUTED_INK, width=1))
    if lo and hi:
        fig.add_shape(type="rect", x0=max(math.log10(lo), lo_x), x1=min(math.log10(hi), hi_x),
                      y0=-0.3, y1=0.3, line_width=0, fillcolor="rgba(195,194,183,0.18)")
    if ratio:
        x = min(max(math.log10(ratio), lo_x), hi_x)
        color = (DIVERGING_POSITIVE if ratio > 1.05 else DIVERGING_NEGATIVE if ratio < 0.95
                 else SECONDARY_INK)
        fig.add_trace(go.Scatter(
            x=[x], y=[0.55], mode="markers+text",
            text=[f"{ratio:.2f}x" + ("+" if ratio > top else "")],
            textposition="top center", textfont=dict(color=PRIMARY_INK, size=13),
            marker=dict(symbol="triangle-down", size=16, color=color,
                        line=dict(color=CHART_SURFACE, width=2)),
            hovertemplate=f"IV / forecast realised vol {ratio:.2f}<extra></extra>"))
    fig.update_layout(**PLOTLY_TEMPLATE["layout"])
    fig.update_layout(
        height=120, margin=dict(l=10, r=10, t=28, b=8), showlegend=False,
        title=dict(text="IV / forecast realised vol (log scale)",
                   font=dict(size=13, color=SECONDARY_INK), x=0.01),
        xaxis=dict(range=[lo_x - 0.09, hi_x + 0.09],
                   tickvals=[lo_x, 0.0, hi_x], ticktext=[f"{1 / top:.1f}x cheap", "1.0x fair",
                                                        f"{top:.1f}x rich"],
                   showgrid=False, zeroline=False, tickfont=dict(color=MUTED_INK, size=11)),
        yaxis=dict(range=[-0.6, 1.2], visible=False))
    return fig
