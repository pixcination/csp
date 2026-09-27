"""
Shared Plotly chart builders for the Ticker Detail page. Every chart pulls
its colors from app/theme.py rather than Plotly defaults, and follows the
dataviz skill's mark specs (thin lines, direct labels over legends where a
chart has <=4 series, status colors reserved for pass/fail-type meaning).
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from app.theme import CATEGORICAL, STATUS, PLOTLY_TEMPLATE, MUTED_INK
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
