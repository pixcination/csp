# legacy/ — the retired Phase-2 stack

Moved here in Phase 8 (see `docs/PHASE8_SUMMARY.md`). **Nothing in `app/`,
`analytics/`, `data_sources/`, `core/` or `pipeline/` imports from this
folder.** It stays in the tree for reference and so its tests still run:

    python -m pytest legacy/tests

| File | Replaced by |
|---|---|
| `app/pages/1_Scanner.py` | Command Center + Decisions today; the Screener page (Phase 14) |
| `app/pages/2_Ticker_Detail.py` | Trade Detail page (Phase 14) |
| `app/pages/3_Trade_Log.py` | Paper book (`analytics/paper.py`, Decisions page) |
| `analytics/scoring.py` | EV ranking in `analytics/candidates.py` |
| `analytics/backtest.py` | `analytics/wheel_backtest.py` (the naked-put harness booked assignment as a loss) |
| `analytics/trade_log.py` | `analytics/paper.py` |
| `analytics/data_access.py` | `data_sources/yfinance_sync.load_daily`, `data_sources/chains`; the `stage3_chains` reader now lives privately in `analytics/iv_history.py` |

The legacy `positions` table in `data/trade_log.duckdb` was empty when
retired; `analytics.paper.migrate_legacy_trade_log()` copies any rows into the
paper book and is safe to re-run.

## Chart builders to reuse in the Trade Detail page (Phase 13/14)

Ticker Detail used these builders from `app/components/charts.py` (which stays
in the app). All are single-leg short-put today; each needs generalising to a
`Position` of any number of legs:

| Builder | Ticker Detail use | Phase 14 tab | Generalisation needed |
|---|---|---|---|
| `price_vol_chart` | price + SMA20 ± 2σ band, strike/expiry marked | *Chart* | MA set from `indicators.py`, respected levels, several strike lines, EM cone, event markers, weekly candles |
| `iv_vs_rv_chart` | IV history vs realized vol | *Context* | add tasty IVR/IVP series (Phase 9) |
| `greeks_over_time_chart` | net Greeks from now to expiry | *Greeks* | sum Greeks over legs |
| `pnl_at_expiration_chart` | short-put payoff, breakeven | *Payoff* | multi-leg payoff, several breakevens |
| `pnl_over_time_chart` | P&L path under flat/±1σ | *Payoff* (T+n curves) | reprice every leg |
| `probability_cone_chart` | 1σ/2σ cone to expiry | *Expected move* | IV and straddle EM bands (§B.5) |
| `chain_volume_oi_chart` | volume/OI by strike | *Chain & liquidity* | highlight every leg, OI walls |
