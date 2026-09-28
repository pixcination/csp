"""
Per-ticker context every trade row carries (Phase 12, roadmap C.4 task 6).

Built once per ticker per run, then applied to each CSP or PCS row:

* **Expected move** for the row's expiration (all three methods, the
  configured one as `em`) and the short strike's distance in EM units.
* **Nearest support** below the short strike -- the nearest studied level
  and the nearest STRONG one (Phase 10 stats: n, hold rate, edge over the
  placebo, median pierce), from the cached support map.
* **Premium-opportunity flags**: IV percentile, short-leg IV/RV, and for
  PCS credit/width -- each against config.yaml -> premium_flags / pcs.
* **Liquidity**: the position's fillability and its weakest leg, and the
  nearest OI wall at or below the short strike.
* **Strike rule**: which rule chose the short strike and why (CSP rows:
  the delta band, which is how CSP strikes are chosen).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from analytics import expected_move as em_mod
from analytics import liquidity
from core.paths import load_config


@dataclass
class TickerContext:
    ticker: str
    spot: float
    today: dt.date
    chain: pd.DataFrame
    ems: dict = field(default_factory=dict)          # (expiration str, root) -> ExpectedMove
    support: pd.DataFrame = field(default_factory=pd.DataFrame)
    atr: float | None = None
    metrics: dict = field(default_factory=dict)
    settlement: str = "physical"
    exercise: str = "american"

    @classmethod
    def build(cls, ticker: str, chain: pd.DataFrame, spot: float, today: dt.date,
              metrics: dict | None = None) -> "TickerContext":
        from analytics import technical_study
        ctx = cls(ticker=ticker, spot=spot, today=today, chain=chain, metrics=metrics or {})
        try:
            from data_sources import universe
            reg = universe.get(ticker) or {}
            ctx.settlement = reg.get("settlement") or "physical"
            ctx.exercise = reg.get("exercise") or "american"
        except Exception:
            pass
        try:
            ctx.support = technical_study.load_support(ticker)
            latest = technical_study.load_latest(ticker)
            if not latest.empty and "atr" in latest:
                ctx.atr = float(latest["atr"].iloc[0])
        except Exception:
            pass
        return ctx

    @property
    def cash_settled(self) -> bool:
        return self.settlement == "cash"

    def group(self, expiration, root=None) -> pd.DataFrame:
        frame = self.chain
        exp = pd.Timestamp(expiration)
        mask = pd.to_datetime(frame["expiration"]) == exp
        if root is not None and "root_symbol" in frame:
            mask &= frame["root_symbol"] == root
        return frame[mask]

    def expected_move(self, expiration, root=None) -> em_mod.ExpectedMove | None:
        key = (str(pd.Timestamp(expiration).date()), root)
        if key not in self.ems:
            group = self.group(expiration, root)
            if group.empty:
                self.ems[key] = None
            else:
                dte = (pd.Timestamp(expiration).date() - self.today).days
                self.ems[key] = em_mod.for_expiration(group, self.spot, dte)
        return self.ems[key]

    def support_below(self, strike: float) -> dict:
        """Nearest studied level and nearest strong level at or below `strike`."""
        out = {}
        if self.support is None or self.support.empty:
            return out
        below = self.support[self.support["level"] <= strike].sort_values("level", ascending=False)
        if not below.empty:
            row = below.iloc[0]
            out.update({"support_level_id": row["level_id"], "support_level": float(row["level"]),
                        "support_status": row.get("status"),
                        "support_summary": row.get("summary")})
        strong = below[below["strong"].fillna(False).astype(bool)]
        if not strong.empty:
            row = strong.iloc[0]
            out.update({"strong_support_id": row["level_id"],
                        "strong_support_level": float(row["level"]),
                        "strong_support_edge_ci_lo": row.get("edge_ci_lo"),
                        "strong_support_pierce_atr": row.get("median_pierce_atr"),
                        "strong_support_summary": row.get("summary")})
        return out

    def strongest_support(self) -> dict | None:
        """The strong level below spot with the highest edge CI lower bound."""
        if self.support is None or self.support.empty:
            return None
        strong = self.support[self.support["strong"].fillna(False).astype(bool)
                              & (self.support["level"] < self.spot)]
        if strong.empty:
            return None
        row = strong.sort_values("edge_ci_lo", ascending=False).iloc[0]
        return row.to_dict()

    # --- Row annotation ------------------------------------------------------

    def annotate(self, row: dict) -> dict:
        """Add EM, support, liquidity and premium-flag context to a trade row."""
        cfg = load_config()
        flags_cfg = cfg.get("premium_flags", {}) or {}
        root = row.get("root_symbol")
        em = self.expected_move(row["expiration"], root)
        if em is not None:
            row.update({"em": em.em, "em_method": em.method, "em_tastytrade": em.em_tastytrade,
                        "em_iv": em.em_iv, "em_straddle": em.em_straddle,
                        "atm_iv": em.atm_iv, "straddle": em.straddle,
                        "short_distance_em": em.distance(row["strike"]),
                        "em_lower_1x": em.spot - em.em, "em_lower_2x": em.spot - 2 * em.em})
        row.update(self.support_below(float(row["strike"])))

        group = self.group(row["expiration"], root)
        wall = liquidity.nearest_wall_below(group, float(row["strike"])) if not group.empty else None
        if wall:
            row.update({"oi_wall_strike": wall["strike"], "oi_wall_oi": wall["open_interest"],
                        "oi_wall_multiple": wall["multiple"]})

        flags = []
        ivp = row.get("ivp")
        if ivp is not None and np.isfinite(ivp) and ivp >= flags_cfg.get("min_ivp", 0.5):
            flags.append(f"IVP {ivp:.0%}")
        ratio = row.get("iv_rv_ratio")
        if ratio is not None and np.isfinite(ratio) and ratio >= flags_cfg.get("min_iv_rv", 1.2):
            flags.append(f"IV/RV {ratio:.2f}")
        cw = row.get("credit_width")
        if cw is not None and np.isfinite(cw) and cw >= (cfg.get("pcs", {}) or {}).get(
                "credit_width_floor", 1 / 3):
            flags.append(f"credit/width {cw:.2f}")
        row["premium_flags"] = "; ".join(flags)
        row["premium_opportunity"] = bool(flags)
        row["settlement"] = self.settlement
        return row


def csp_context(ctx: TickerContext, row: dict, chain_row: pd.Series | None = None) -> dict:
    """Phase 12 context for a CSP row, added AFTER `evaluate_strike` so its
    numbers are untouched."""
    lo, hi = row.get("_delta_range", (None, None))
    row.setdefault("strike_rule", "delta")
    row.setdefault("strike_rule_reason",
                   f"CSP strikes: every put with delta {row.get('delta') or 0:+.2f} inside "
                   f"the requested band" + (f" [{lo:+.2f}, {hi:+.2f}]" if lo is not None else ""))
    if chain_row is not None:
        leg = liquidity.leg(chain_row, "put")
        row.update({"fillability": leg.fillability, "weakest_leg": f"put {leg.strike:g}",
                    "spread_pct": leg.spread_pct})
    row.pop("_delta_range", None)
    return ctx.annotate(row)
