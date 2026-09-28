"""
Outlook v1 (Phase 20, review Part D): three dials per symbol and horizon.

    Direction  0-10  P(up by more than 1/4 EM) - P(down by more than 1/4 EM),
                     5 = balanced
    Range      0-10  P(inside +/-1 EM) against the stock's own base rate,
                     5 = its normal
    Volatility 0-10  implied vol vs the realised vol the engine forecasts,
                     5 = fair, above 5 = IV rich (credit), below = cheap (debit)

on the horizon grid 3, 5, 7, 10, 14, 21, 30, 45, 60 calendar days (converted
to trading days, x 252/365). Display and filter only: nothing here ranks or
gates a trade (D.5 step 3 waits for tracked outcomes).

WHAT "EM" MEANS HERE
    The Direction and Range events are measured in the expected move from
    20-day REALISED vol (RV20 x sqrt(trading days / 252)), not implied vol:
    that is the only move unit with 20+ years of history, so every one of
    these probabilities is tested walk-forward. The IV-based EM needs IV
    history (Phase 18's archive is building it); the live rows also carry
    P(inside the IV EM) from the engine, for reference.

TWO MODELS, BLENDED (D.2)
    engine    the probability engine's H and T paths (analytics/prob_engine:
              vol-conditioned block bootstrap, and H restricted to today's
              technical state) read at each horizon: P(up), P(down),
              P(inside), and the path vol, the realised-vol forecast the
              Volatility dial uses (conditioning on today's RV makes the
              forecast carry the mean reversion history shows).
    logistic  ridge-regularised logistic regression POOLED across symbols
              on price-based, point-in-time features (vol-normalised
              momentum 1w/1m/3m/12-1m, distance from the 50D and 200D, the
              50/200 cross, RSI, ADX, RV20, its 1-year rank, RV20/RV60, SPY's
              1-month momentum and RV) plus the symbol's own base rate. One
              model per horizon and event (up, down, inside).
    The dial's probability is the mean of the two. Option features (put/call
    ratios, skew, term slope) and days to earnings wait for the archive.

SKILL AND CONFIDENCE (D.1)
    `validate()` runs the walk-forward: refit every January on everything
    whose outcome was known by then, predict that year weekly. Skill is the
    Brier skill score vs the stock's own point-in-time base rate, per
    symbol, horizon and event, for the blend and each component ("hist" is
    the engine's H analogue: the frequency among past days at a similar RV).
    Confidence combines (a) the effective sample (weekly predictions / the
    horizon in weeks), (b) that skill, (c) the agreement of the two models.
    A symbol's own skill is noisy (70-600 effective predictions), so the
    skill used is pulled toward the pooled one: (n_eff x own + K x pooled) /
    (n_eff + K), K = `skill_prior_n` (300) -- `bss_shrunk` in the table.
    A dial is SHRUNK toward 5 by that skill: full length at `skill_full` (0.03),
    none at 0, and its band widens as the shrink grows -- where skill is ~0
    the arrow sits at neutral with a wide band (the honest answer for
    Direction on most names). Volatility is not walk-forward tested (it
    needs IV history); Phase 14 measured vol-rank predicting premium kept
    (IC +0.14), so it carries "not tested here" rather than a skill number.

FILES
    data/outlook/model.json              the final fit (all data), per horizon/event
    data/outlook/latest.parquet          the live table (+ dated copies)
    data/validation/outlook_skill.parquet  walk-forward skill
"""
from __future__ import annotations

import datetime as dt
import json
import math
import zlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.paths import data_dir, load_config, validation_dir

HORIZONS = (3, 5, 7, 10, 14, 21, 30, 45, 60)
EVENTS = ("up", "down", "inside")
FEATURES = ("mom_5", "mom_21", "mom_63", "mom_12_1", "dist_50", "dist_200", "cross_50_200",
            "rsi", "adx", "rv20", "rv_rank", "rv_ratio", "spy_mom_21", "spy_rv20", "base_logit")
LEVELS = ("none", "low", "medium", "high")
DIALS = ("direction", "range", "volatility")


def cfg() -> dict:
    base = {"horizons": list(HORIZONS), "history_start": "2000-01-01", "test_start_year": 2015,
            "step_days": 5, "l2": 10.0, "base_window_days": 2520, "min_base_days": 250,
            "hist_band": 0.25, "hist_min_days": 100, "skill_full": 0.03, "skill_none": 0.005,
            "n_paths": 4000, "refit_days": 7, "vol_full_ratio": 1.5, "min_n_eff": 30,
            "skill_prior_n": 300}
    base.update(load_config().get("outlook", {}) or {})
    return base


def steps(horizon_days: int) -> int:
    """Calendar days -> trading days on the grid (x 252/365, at least 1)."""
    return max(int(round(float(horizon_days) * 252.0 / 365.0)), 1)


def folder():
    path = data_dir() / "outlook"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


# --- Features and labels (point in time) ----------------------------------------------------

def _rv(log_ret: pd.Series, n: int) -> pd.Series:
    return log_ret.rolling(n, min_periods=n).std() * math.sqrt(252.0)


def features(daily: pd.DataFrame, spy: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per day: the model features (vol-normalised, known at the
    close), the readable values behind them (`raw_*`), close and RV20."""
    from analytics import indicators
    d = daily.sort_values("date").reset_index(drop=True)
    d["date"] = pd.to_datetime(d["date"])
    c = d["close"].astype(float)
    lr = np.log(c).diff()
    rv20, rv60 = _rv(lr, 20), _rv(lr, 60)
    s = lambda k: rv20 * math.sqrt(k / 252.0)  # noqa: E731  (one period's typical move)
    out = pd.DataFrame({"date": d["date"], "close": c, "rv20_level": rv20})
    for k in (5, 21, 63):
        out[f"mom_{k}"] = np.log(c / c.shift(k)) / s(k)
    out["mom_12_1"] = np.log(c.shift(21) / c.shift(252)) / (rv60 * math.sqrt(231 / 252.0))
    ma50 = c.rolling(50, min_periods=50).mean()
    ma200 = c.rolling(200, min_periods=200).mean()
    out["dist_50"] = np.log(c / ma50) / s(21)
    out["dist_200"] = np.log(c / ma200) / s(63)
    out["cross_50_200"] = np.log(ma50 / ma200) / s(63)
    rsi = indicators.rsi(c, 14)
    out["rsi"] = (rsi - 50.0) / 50.0
    try:
        adx = indicators.adx(d, 14)["adx"]
    except Exception:
        adx = pd.Series(np.nan, index=d.index)
    out["adx"] = adx / 100.0
    out["rv20"] = rv20
    out["rv_rank"] = rv20.rolling(252, min_periods=126).rank(pct=True)
    out["rv_ratio"] = np.log(rv20 / rv60)
    out["raw_rsi"], out["raw_adx"] = rsi, adx
    out["raw_vs_50"], out["raw_vs_200"] = c / ma50 - 1.0, c / ma200 - 1.0
    out["raw_mom_21"], out["raw_mom_63"] = c / c.shift(21) - 1.0, c / c.shift(63) - 1.0
    if spy is not None and not spy.empty:
        out = out.merge(spy, on="date", how="left")
    else:
        out["spy_mom_21"], out["spy_rv20"] = np.nan, np.nan
    return out.replace([np.inf, -np.inf], np.nan)


def spy_features(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.sort_values("date").reset_index(drop=True)
    c = d["close"].astype(float)
    lr = np.log(c).diff()
    rv20 = _rv(lr, 20)
    return pd.DataFrame({"date": pd.to_datetime(d["date"]),
                         "spy_mom_21": np.log(c / c.shift(21)) / (rv20 * math.sqrt(21 / 252.0)),
                         "spy_rv20": rv20})


def labels(frame: pd.DataFrame, horizon_days: int) -> pd.DataFrame:
    """The three events over `horizon_days` (NaN while the future is unknown)."""
    td = steps(horizon_days)
    r = np.log(frame["close"].shift(-td) / frame["close"])
    em = frame["rv20_level"] * math.sqrt(td / 252.0)
    known = r.notna() & em.notna()
    out = pd.DataFrame({"up": (r > em / 4.0), "down": (r < -em / 4.0),
                        "inside": (r.abs() < em)}).astype(float)
    return out.where(known)


def base_rates(lab: pd.DataFrame, horizon_days: int, c: dict | None = None) -> pd.DataFrame:
    """Point-in-time base rate of each event: its frequency over the last
    `base_window_days` among outcomes already known (start <= t - td)."""
    c = c or cfg()
    td = steps(horizon_days)
    return lab.shift(td).rolling(int(c["base_window_days"]),
                                 min_periods=int(c["min_base_days"])).mean()


# --- Ridge logistic regression (Newton) -----------------------------------------------------

@dataclass
class Logit:
    features: list
    mean: list
    std: list
    coef: list
    intercept: float
    n: int = 0

    def z(self, frame: pd.DataFrame) -> np.ndarray:
        x = frame[self.features].to_numpy(float)
        z = (x - np.asarray(self.mean)) / np.asarray(self.std)
        return np.clip(np.nan_to_num(z, nan=0.0), -5.0, 5.0)

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        eta = self.intercept + self.z(frame) @ np.asarray(self.coef)
        return 1.0 / (1.0 + np.exp(-eta))

    def to_dict(self) -> dict:
        return {"features": list(self.features), "mean": list(map(float, self.mean)),
                "std": list(map(float, self.std)), "coef": list(map(float, self.coef)),
                "intercept": float(self.intercept), "n": int(self.n)}


def fit_logit(frame: pd.DataFrame, y: np.ndarray, l2: float = 10.0,
              feature_names=FEATURES, iters: int = 25) -> Logit:
    """Minimise log loss + l2/2 |beta|^2 (intercept unpenalised) by Newton
    steps on standardised features (missing -> 0 = the mean)."""
    names = list(feature_names)
    x = frame[names].to_numpy(float)
    mean = np.nanmean(x, axis=0)
    std = np.nanstd(x, axis=0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    std = np.where(np.isfinite(std) & (std > 1e-9), std, 1.0)
    z = np.clip(np.nan_to_num((x - mean) / std, nan=0.0), -5.0, 5.0)
    design = np.column_stack([np.ones(len(z)), z])
    beta = np.zeros(design.shape[1])
    p0 = float(np.clip(np.mean(y), 1e-3, 1 - 1e-3))
    beta[0] = math.log(p0 / (1 - p0))
    penalty = np.full(design.shape[1], float(l2))
    penalty[0] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(design @ beta)))
        grad = design.T @ (p - y) + penalty * beta
        hess = (design * (p * (1 - p))[:, None]).T @ design + np.diag(penalty + 1e-9)
        step = np.linalg.solve(hess, grad)
        beta -= step
        if np.max(np.abs(step)) < 1e-7:
            break
    return Logit(names, mean.tolist(), std.tolist(), beta[1:].tolist(), float(beta[0]), len(y))


# --- The panel ---------------------------------------------------------------------------

def panel(symbols: list[str], loader=None, c: dict | None = None) -> dict[str, pd.DataFrame]:
    """Per symbol: features, and per horizon the events and their base rates
    (columns `{event}_{h}`, `base_{event}_{h}`)."""
    c = c or cfg()
    if loader is None:
        from data_sources.yfinance_sync import load_daily

        def loader(symbol):
            return load_daily(symbol, basis="price")
    spy_daily = loader("SPY")
    spy = spy_features(spy_daily) if spy_daily is not None and not spy_daily.empty else None
    start = pd.Timestamp(c["history_start"])
    out = {}
    for symbol in symbols:
        daily = loader(symbol)
        if daily is None or daily.empty or len(daily) < 300:
            continue
        frame = features(daily, spy)
        for h in c["horizons"]:
            lab = labels(frame, h)
            base = base_rates(lab, h, c)
            for e in EVENTS:
                frame[f"{e}_{h}"] = lab[e]
                frame[f"base_{e}_{h}"] = base[e]
        frame = frame[frame["date"] >= start - pd.DateOffset(years=1)].reset_index(drop=True)
        frame["symbol"] = symbol
        out[symbol] = frame
    return out


def _design(frame: pd.DataFrame, event: str, h: int) -> pd.DataFrame:
    x = frame[[f for f in FEATURES if f != "base_logit"]].copy()
    x["base_logit"] = _logit(frame[f"base_{event}_{h}"].fillna(0.5))
    return x


# --- Historical analogue (the engine's H, point in time) ------------------------------------

def hist_probs(frame: pd.DataFrame, test_idx: np.ndarray, c: dict) -> dict[int, np.ndarray]:
    """For each test day t and horizon: the frequency of each event among
    the last `base_window_days` start days whose outcome was known by t
    (s <= t - td) and whose RV20 was within +/- hist_band of t's; the plain
    base rate when fewer than hist_min_days match. {h: (len(test), 3)}."""
    rv = frame["rv20"].to_numpy(float)
    window, band, need = int(c["base_window_days"]), float(c["hist_band"]), int(c["hist_min_days"])
    out = {}
    labs = {h: frame[[f"{e}_{h}" for e in EVENTS]].to_numpy(float) for h in c["horizons"]}
    for h in c["horizons"]:
        out[h] = np.full((len(test_idx), 3), np.nan)
    for i, t in enumerate(test_idx):
        lo = max(0, t - window)
        seg_rv = rv[lo:t]
        if not np.isfinite(rv[t]) or not len(seg_rv):
            continue
        similar = np.abs(seg_rv / rv[t] - 1.0) <= band
        for h in c["horizons"]:
            td = steps(h)
            end = t - td + 1 - lo
            if end <= 0:
                continue
            lab = labs[h][lo:lo + end]
            known = np.isfinite(lab[:, 0])
            mask = similar[:end] & known
            if mask.sum() >= need:
                out[h][i] = lab[mask].mean(axis=0)
            elif known.sum() >= int(c["min_base_days"]):
                out[h][i] = lab[known].mean(axis=0)
    return out


# --- Walk-forward validation ----------------------------------------------------------------

def brier_skill(p: np.ndarray, y: np.ndarray, base: np.ndarray) -> tuple[float, float, float]:
    ok = np.isfinite(p) & np.isfinite(y) & np.isfinite(base)
    if not ok.any():
        return float("nan"), float("nan"), float("nan")
    b = float(np.mean((p[ok] - y[ok]) ** 2))
    b0 = float(np.mean((base[ok] - y[ok]) ** 2))
    return (1.0 - b / b0 if b0 > 0 else float("nan")), b, b0


def validate(symbols: list[str], loader=None, reporter=None, c: dict | None = None,
             save: bool = True) -> dict:
    """Walk-forward skill and the final model. Returns {skill, model,
    predictions}; with `save`, writes model.json and outlook_skill.parquet."""
    from core.progress import NullReporter
    c = c or cfg()
    reporter = reporter or NullReporter()
    with reporter.stage("outlook_panel", "Outlook: features and outcomes", total=1):
        data = panel(symbols, loader, c)
        reporter.advance(1, note=f"{len(data)} symbols")
    if not data:
        return {"skill": pd.DataFrame(), "model": {}, "predictions": pd.DataFrame()}
    step = int(c["step_days"])
    start = pd.Timestamp(c["history_start"])
    pooled = []
    tests = {}
    for symbol, frame in data.items():
        frame = frame.copy()
        idx = np.arange(len(frame))
        frame["row"] = idx
        frame["train_row"] = (idx % step == 0) & (frame["date"] >= start)
        frame["test_row"] = (idx % step == 0) & (frame["date"].dt.year >= int(c["test_start_year"]))
        tests[symbol] = frame
        pooled.append(frame)
    pool = pd.concat(pooled, ignore_index=True)
    years = sorted(pool.loc[pool["test_row"], "date"].dt.year.unique())
    preds = []
    with reporter.stage("outlook_hist", "Outlook: historical analogue", total=len(tests)):
        hist = {}
        for symbol, frame in tests.items():
            test_idx = frame.index[frame["test_row"]].to_numpy()
            hist[symbol] = (test_idx, hist_probs(frame, test_idx, c))
            reporter.advance(1, note=symbol)
    with reporter.stage("outlook_fit", "Outlook: walk-forward fits",
                        total=len(years) * len(c["horizons"])):
        for year in years:
            cutoff = pd.Timestamp(year=int(year), month=1, day=1)
            test_mask = pool["test_row"] & (pool["date"].dt.year == year)
            for h in c["horizons"]:
                td = steps(h)
                # An outcome is known by the cutoff when its horizon has
                # ended: td trading days after the start (calendar proxy).
                ends = pool["date"] + pd.to_timedelta(int(math.ceil(td * 365 / 252)) + 1, "D")
                train = pool[pool["train_row"] & (ends < cutoff)]
                test = pool[test_mask]
                for e in EVENTS:
                    col = f"{e}_{h}"
                    tr = train[train[col].notna()]
                    if len(tr) < 500 or test.empty:
                        continue
                    model = fit_logit(_design(tr, e, h), tr[col].to_numpy(float), c["l2"])
                    p = model.predict(_design(test, e, h))
                    preds.append(pd.DataFrame({
                        "symbol": test["symbol"].to_numpy(), "row": test["row"].to_numpy(),
                        "date": test["date"].to_numpy(), "horizon": h, "event": e,
                        "logit": p, "y": test[col].to_numpy(float),
                        "base": test[f"base_{e}_{h}"].to_numpy(float)}))
                reporter.advance(1, note=f"{year} {h}d")
    predictions = pd.concat(preds, ignore_index=True) if preds else pd.DataFrame()
    if not predictions.empty:
        hist_rows = []
        for symbol, (test_idx, probs) in hist.items():
            for h, arr in probs.items():
                for j, e in enumerate(EVENTS):
                    hist_rows.append(pd.DataFrame({"symbol": symbol, "row": test_idx,
                                                   "horizon": h, "event": e,
                                                   "hist": arr[:, j]}))
        predictions = predictions.merge(pd.concat(hist_rows, ignore_index=True),
                                        on=["symbol", "row", "horizon", "event"], how="left")
        predictions["blend"] = predictions[["logit", "hist"]].mean(axis=1)
    skill = skill_table(predictions)
    model = {}
    with reporter.stage("outlook_final", "Outlook: final fit", total=len(c["horizons"])):
        final = pool[pool["train_row"]]
        for h in c["horizons"]:
            for e in EVENTS:
                tr = final[final[f"{e}_{h}"].notna()]
                if len(tr) >= 500:
                    model[f"{h}|{e}"] = fit_logit(_design(tr, e, h), tr[f"{e}_{h}"].to_numpy(float),
                                                  c["l2"]).to_dict()
            reporter.advance(1, note=f"{h}d")
    meta = {"fitted_at": dt.datetime.now().isoformat(timespec="seconds"),
            "symbols": sorted(data), "history_start": c["history_start"],
            "test_start_year": c["test_start_year"], "l2": c["l2"], "step_days": step}
    if save:
        (folder() / "model.json").write_text(json.dumps({"meta": meta, "models": model},
                                                        indent=1), encoding="utf-8")
        skill.to_parquet(validation_dir() / "outlook_skill.parquet", index=False)
    return {"skill": skill, "model": model, "meta": meta, "predictions": predictions}


def skill_table(predictions: pd.DataFrame) -> pd.DataFrame:
    """Brier skill vs the base rate per symbol x horizon x event x component,
    plus pooled rows (symbol ALL). n_eff = weekly predictions / horizon in
    weeks (overlapping outcomes are not independent)."""
    if predictions is None or predictions.empty:
        return pd.DataFrame(columns=["symbol", "horizon", "event", "component", "bss",
                                     "brier", "brier_base", "n", "n_eff"])
    rows = []
    comps = [c for c in ("blend", "logit", "hist") if c in predictions]
    for (symbol, h, e), g in list(predictions.groupby(["symbol", "horizon", "event"])) + \
            [(("ALL", h, e), g) for (h, e), g in predictions.groupby(["horizon", "event"])]:
        y, base = g["y"].to_numpy(float), g["base"].to_numpy(float)
        n = int((np.isfinite(y) & np.isfinite(base)).sum())
        weeks = max(steps(h) / 5.0, 1.0)
        for comp in comps:
            bss, b, b0 = brier_skill(g[comp].to_numpy(float), y, base)
            rows.append({"symbol": symbol, "horizon": int(h), "event": e, "component": comp,
                         "bss": bss, "brier": b, "brier_base": b0, "n": n,
                         "n_eff": n / weeks if symbol != "ALL" else n / weeks})
    table = pd.DataFrame(rows)
    pooled = table[table["symbol"] == "ALL"].set_index(["horizon", "event", "component"])["bss"]
    k = float(cfg()["skill_prior_n"])
    prior = table.set_index(["horizon", "event", "component"]).index.map(pooled.to_dict())
    own_n = table["n_eff"].clip(lower=0)
    table["bss_shrunk"] = np.where(table["symbol"] == "ALL", table["bss"],
                                   (own_n * table["bss"] + k * np.asarray(prior, float))
                                   / (own_n + k))
    return table


def load_model() -> dict:
    try:
        return json.loads((folder() / "model.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_skill() -> pd.DataFrame:
    path = validation_dir() / "outlook_skill.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def model_age_days() -> float | None:
    fitted = (load_model().get("meta") or {}).get("fitted_at")
    try:
        return (dt.datetime.now() - dt.datetime.fromisoformat(fitted)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


# --- Scores -------------------------------------------------------------------------------

def shrink_for(skill: float | None, c: dict) -> float:
    if skill is None or not np.isfinite(skill):
        return 0.0
    return float(np.clip((skill - c["skill_none"]) / (c["skill_full"] - c["skill_none"]), 0, 1))


def direction_raw(p_up: float, p_down: float) -> float:
    return float(np.clip(5.0 + 5.0 * (p_up - p_down), 0.0, 10.0))


def range_raw(p_in: float, base: float) -> float:
    """5 at the stock's base rate, 10 at certainty, 0 at never."""
    if base is None or not np.isfinite(base):
        base = 0.5
    if p_in >= base:
        return float(5.0 + 5.0 * (p_in - base) / max(1.0 - base, 1e-6))
    return float(5.0 - 5.0 * (base - p_in) / max(base, 1e-6))


def vol_raw(iv: float | None, forecast_rv: float | None, c: dict) -> float | None:
    if not iv or not forecast_rv or iv <= 0 or forecast_rv <= 0:
        return None
    x = math.log(iv / forecast_rv) / math.log(float(c["vol_full_ratio"]))
    return float(5.0 + 5.0 * np.clip(x, -1.0, 1.0))


def level(skill: float | None, n_eff: float | None, spread: float, c: dict) -> str:
    """none / low / medium / high from (a) sample, (b) skill, (c) agreement."""
    s = shrink_for(skill, c)
    if s <= 0 or (n_eff is not None and n_eff < c["min_n_eff"]):
        return "none"
    if s < 0.5 or spread > 0.15:
        return "low"
    if s < 1.0 or spread > 0.08 or (n_eff is not None and n_eff < 100):
        return "medium"
    return "high"


def trend_class(direction_score: float | None) -> str | None:
    """The recommender's vocabulary from the (skill-shrunk) Direction dial."""
    if direction_score is None or not np.isfinite(direction_score):
        return None
    return "uptrend" if direction_score >= 6.0 else "downtrend" if direction_score <= 4.0 \
        else "range"


# --- Live ---------------------------------------------------------------------------------

READABLE = {
    "rsi": lambda r: f"RSI {r['raw_rsi']:.0f}",
    "adx": lambda r: f"ADX {r['raw_adx']:.0f}",
    "dist_50": lambda r: f"{r['raw_vs_50']:+.1%} vs 50D",
    "dist_200": lambda r: f"{r['raw_vs_200']:+.1%} vs 200D",
    "cross_50_200": lambda r: "50D above 200D" if r["cross_50_200"] > 0 else "50D below 200D",
    "mom_5": lambda r: "1-week move",
    "mom_21": lambda r: f"1-month {r['raw_mom_21']:+.1%}",
    "mom_63": lambda r: f"3-month {r['raw_mom_63']:+.1%}",
    "mom_12_1": lambda r: "12-1 month momentum",
    "rv20": lambda r: f"RV20 {r['rv20']:.0%}",
    "rv_rank": lambda r: f"RV rank {r['rv_rank']:.2f}",
    "rv_ratio": lambda r: "RV20 vs RV60",
    "spy_mom_21": lambda r: "SPY 1-month",
    "spy_rv20": lambda r: f"SPY RV20 {r['spy_rv20']:.0%}",
    "base_logit": lambda r: "its own base rate",
}


def explain(models: dict, row: pd.DataFrame, h: int, n: int = 3) -> str:
    """What moves Direction: per feature (beta_up - beta_down) x z, top n."""
    up, down = models.get(f"{h}|up"), models.get(f"{h}|down")
    if not up or not down:
        return ""
    mu, md = Logit(**up), Logit(**down)
    x_up = _design(row, "up", h)
    contrib = mu.z(x_up)[0] * np.asarray(mu.coef) - md.z(_design(row, "down", h))[0] * \
        np.asarray(md.coef)
    order = np.argsort(-np.abs(contrib))[:n]
    parts = []
    r = row.iloc[0]
    for i in order:
        name = mu.features[i]
        if abs(contrib[i]) < 0.02:
            continue
        try:
            text = READABLE.get(name, lambda _: name)(r)
        except (TypeError, ValueError, KeyError):
            text = name
        parts.append(f"{text} ({'+' if contrib[i] > 0 else '-'})")
    return ", ".join(parts)


def _iv_at(metrics: dict, h: int) -> float | None:
    """IV for the horizon from the TastyTrade metrics: the 15-day index up to
    15 days, the 30-day beyond 30, linear in between (flat past 30)."""
    iv15, iv30 = metrics.get("iv_index_15d"), metrics.get("iv_index")
    iv15 = iv15 if iv15 and iv15 > 0 else None
    iv30 = iv30 if iv30 and iv30 > 0 else None
    if iv15 and iv30:
        w = float(np.clip((h - 15) / 15.0, 0, 1))
        return (1 - w) * iv15 + w * iv30
    return iv30 or iv15


def engine_reads(daily: pd.DataFrame, horizons, c: dict, ticker: str = "") -> dict:
    """H and T paths read at each horizon: {h: {model: {up, down, inside,
    path_vol}}} plus the engine's effective sample per model."""
    from analytics import prob_engine as pe
    from analytics.probabilities import _technical_frame
    ecfg = pe.EngineConfig.from_config()
    ecfg.n_paths = int(c["n_paths"])
    n_steps = max(steps(h) for h in horizons)
    tech, support = _technical_frame(ticker, daily)
    seed = ecfg.seed + zlib.crc32(f"{ticker}|outlook|{n_steps}".encode())
    paths = pe.ticker_paths(daily, n_steps, ecfg, np.random.default_rng(seed), tech, support)
    closes = daily.sort_values("date")["close"].astype(float)
    rv20 = float(np.log(closes).diff().tail(20).std() * math.sqrt(252.0))
    out = {"meta": {m: {"effective_n": getattr(paths, m).effective_n,
                        "label": getattr(paths, m).label} for m in ("H", "T")},
           "rv20": rv20}
    for h in horizons:
        td = steps(h)
        em = rv20 * math.sqrt(td / 252.0)
        reads = {}
        for m in ("H", "T"):
            ps = getattr(paths, m)
            if ps.log_returns is None:
                continue
            r = ps.log_returns[:, td - 1]
            reads[m] = {"up": float(np.mean(r > em / 4)), "down": float(np.mean(r < -em / 4)),
                        "inside": float(np.mean(np.abs(r) < em)),
                        "path_vol": float(np.std(r) * math.sqrt(252.0 / td)), "r": r}
        out[h] = reads
    return out


def g_down_prob(iv: float | None, h: int, em_iv: float, rate: float = 0.045) -> float | None:
    """G (lognormal at the IV): P(S_h < S_0 (1 - em_iv))."""
    from scipy.stats import norm
    if not iv or em_iv >= 1:
        return None
    t = h / 365.0
    mu = (rate - 0.5 * iv * iv) * t
    return float(norm.cdf((math.log(1 - em_iv) - mu) / (iv * math.sqrt(t))))


def for_ticker(ticker: str, daily: pd.DataFrame, spy: pd.DataFrame | None, models: dict,
               skill: pd.DataFrame, metrics: dict | None, c: dict | None = None,
               today: dt.date | None = None) -> list[dict]:
    """The Outlook rows of one ticker, one per grid horizon."""
    c = c or cfg()
    horizons = list(c["horizons"])
    frame = features(daily, spy)
    for h in horizons:
        lab = labels(frame, h)
        base = base_rates(lab, h, c)
        for e in EVENTS:
            frame[f"base_{e}_{h}"] = base[e]
    last = frame.tail(1).reset_index(drop=True)
    eng = engine_reads(daily, horizons, c, ticker)
    metrics = metrics or {}
    rows = []
    sk = skill if skill is not None else pd.DataFrame()

    def skill_of(h, e):
        if sk.empty:
            return None, None
        pick = sk[(sk["horizon"] == h) & (sk["event"] == e) & (sk["component"] == "blend")]
        mine = pick[pick["symbol"] == ticker]
        chosen = mine if not mine.empty else pick[pick["symbol"] == "ALL"]
        if chosen.empty:
            return None, None
        column = "bss_shrunk" if "bss_shrunk" in chosen else "bss"
        return float(chosen[column].iloc[0]), float(chosen["n_eff"].iloc[0])

    for h in horizons:
        rec = {"ticker": ticker, "horizon": h, "trading_days": steps(h),
               "as_of": pd.Timestamp(last["date"].iloc[0]).date(), "spot": float(last["close"].iloc[0]),
               "rv20": eng["rv20"]}
        reads = eng.get(h, {})
        comps: dict[str, dict] = {}
        if reads:
            comps["engine"] = {e: float(np.mean([reads[m][e] for m in reads])) for e in EVENTS}
        logit = {}
        for e in EVENTS:
            model = models.get(f"{h}|{e}")
            if model:
                logit[e] = float(Logit(**model).predict(_design(last, e, h))[0])
        if len(logit) == 3:
            comps["logit"] = logit
        base = {e: float(last[f"base_{e}_{h}"].iloc[0]) for e in EVENTS}
        if not comps:
            continue
        p = {e: float(np.mean([v[e] for v in comps.values()])) for e in EVENTS}
        for e in EVENTS:
            rec[f"p_{e}"] = p[e]
            rec[f"base_{e}"] = base[e]
            for name, v in comps.items():
                rec[f"p_{e}_{name}"] = v[e]
        for m in ("H", "T"):
            if m in reads:
                rec[f"p_up_{m}"], rec[f"p_down_{m}"] = reads[m]["up"], reads[m]["down"]

        # Direction
        s_up, n_up = skill_of(h, "up")
        s_dn, n_dn = skill_of(h, "down")
        d_skill = None if s_up is None or s_dn is None else (s_up + s_dn) / 2.0
        d_neff = None if n_up is None else n_up
        spread = (abs((comps["engine"]["up"] - comps["engine"]["down"])
                      - (comps["logit"]["up"] - comps["logit"]["down"])) / 2.0
                  if len(comps) == 2 else 0.0)
        raw = direction_raw(p["up"], p["down"])
        shrink = shrink_for(d_skill, c)
        score = 5.0 + (raw - 5.0) * shrink
        half = 0.5 + 2.5 * (1.0 - shrink) + 10.0 * spread
        rec.update({"direction_raw": raw, "direction": score,
                    "direction_lo": max(score - half, 0.0), "direction_hi": min(score + half, 10.0),
                    "direction_base": direction_raw(base["up"], base["down"]),
                    "direction_skill": d_skill, "direction_n_eff": d_neff,
                    "direction_conf": level(d_skill, d_neff, spread, c),
                    "direction_why": explain(models, last, h)})
        # Range
        r_skill, r_neff = skill_of(h, "inside")
        spread_r = abs(comps["engine"]["inside"] - comps["logit"]["inside"]) \
            if len(comps) == 2 else 0.0
        raw_r = range_raw(p["inside"], base["inside"])
        shrink_r = shrink_for(r_skill, c)
        score_r = 5.0 + (raw_r - 5.0) * shrink_r
        half_r = 0.5 + 2.5 * (1.0 - shrink_r) + 10.0 * spread_r
        rec.update({"range_raw": raw_r, "range": score_r, "range_lo": max(score_r - half_r, 0.0),
                    "range_hi": min(score_r + half_r, 10.0), "range_base": 5.0,
                    "range_skill": r_skill, "range_n_eff": r_neff,
                    "range_conf": level(r_skill, r_neff, spread_r, c)})
        # Volatility
        iv = _iv_at(metrics, h)
        vols = [reads[m]["path_vol"] for m in reads]
        frv = float(np.mean(vols)) if vols else None
        v = vol_raw(iv, frv, c)
        v_spread = (abs(vols[0] - vols[1]) / max(np.mean(vols), 1e-6)) if len(vols) == 2 else 0.3
        rec.update({"iv": iv, "forecast_rv": frv, "volatility": v,
                    "volatility_lo": None if v is None else max(v - 1.0 - 5 * v_spread, 0.0),
                    "volatility_hi": None if v is None else min(v + 1.0 + 5 * v_spread, 10.0),
                    "volatility_base": 5.0, "volatility_skill": None,
                    "volatility_conf": "none" if v is None else
                    ("medium" if v_spread < 0.15 else "low")})
        # The IV-based move, for reference, and G vs T
        if iv:
            em_iv = iv * math.sqrt(h / 365.0)
            rec["em_iv_pct"] = em_iv
            if reads:
                # The same H/T average the dials use (T alone rests on few
                # similar days and strings calm blocks together).
                moves = [np.expm1(reads[m]["r"]) for m in reads]
                rec["p_inside_iv_em"] = float(np.mean([np.mean(np.abs(x) < em_iv) for x in moves]))
                rec["p_down_iv_em_hist"] = float(np.mean([np.mean(x < -em_iv) for x in moves]))
                rec["p_down_iv_em_G"] = g_down_prob(iv, h, em_iv)
                rec["hist_model"] = "/".join(sorted(reads))
        rec["engine_effective_n"] = min((eng["meta"][m]["effective_n"] for m in reads),
                                        default=0)
        rows.append(rec)
    return rows


def divergence_sentence(rec: dict) -> str | None:
    """'The market prices more downside than this setup has historically
    produced' -- G vs T (or H) for a drop beyond the IV expected move."""
    g, t = rec.get("p_down_iv_em_G"), rec.get("p_down_iv_em_hist")
    if g is None or t is None or not np.isfinite(g) or not np.isfinite(t):
        return None
    model = ("this stock at this volatility and technical setup" if "T" in str(rec.get("hist_model"))
             else "this stock at this volatility")
    move = f"{rec['em_iv_pct']:.1%}"
    if g > t * 1.25 and g - t > 0.02:
        return (f"The market prices more downside than {model} has historically produced: "
                f"P(down more than {move} in {rec['horizon']}d) {g:.0%} implied vs {t:.0%}.")
    if t > g * 1.25 and t - g > 0.02:
        return (f"{model[0].upper() + model[1:]} has historically fallen more than {move} in "
                f"{rec['horizon']}d {t:.0%} of the time, above the {g:.0%} the options imply.")
    return (f"Implied and historical downside agree: P(down more than {move} in "
            f"{rec['horizon']}d) {g:.0%} implied vs {t:.0%}.")


def build(tickers: list[str], reporter=None, c: dict | None = None, save: bool = True,
          loader=None, metrics_source=None) -> pd.DataFrame:
    """The live Outlook for `tickers` on the grid; writes latest.parquet."""
    from core.progress import NullReporter
    c = c or cfg()
    reporter = reporter or NullReporter()
    if loader is None:
        from data_sources.yfinance_sync import load_daily

        def loader(symbol):
            return load_daily(symbol, basis="price")
    if metrics_source is None:
        from data_sources import tasty_metrics

        def metrics_source(symbol):
            try:
                return tasty_metrics.for_symbol(symbol) or {}
            except Exception:
                return {}
    models = load_model().get("models") or {}
    skill = load_skill()
    spy_daily = loader("SPY")
    spy = spy_features(spy_daily) if spy_daily is not None and not spy_daily.empty else None
    rows = []
    with reporter.stage("outlook", "Outlook dials", total=len(tickers)):
        for ticker in tickers:
            try:
                daily = loader(ticker)
                if daily is None or len(daily) < 300:
                    reporter.advance(1, note=f"{ticker}: not enough history")
                    continue
                rows += for_ticker(ticker, daily, spy, models, skill, metrics_source(ticker), c)
                reporter.advance(1, note=ticker)
            except Exception as exc:
                reporter.advance(1, note=f"{ticker}: {exc}")
    frame = pd.DataFrame(rows)
    if save and not frame.empty:
        frame.to_parquet(folder() / "latest.parquet", index=False)
        day = str(frame["as_of"].max())
        frame.to_parquet(folder() / f"{day}.parquet", index=False)
    return frame


def load_latest() -> pd.DataFrame:
    path = folder() / "latest.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def at(table: pd.DataFrame, ticker: str, dte: float) -> dict | None:
    """One ticker's dials at any DTE: linear between the grid horizons
    around it (clamped at the ends); text fields from the nearer one."""
    if table is None or table.empty:
        return None
    mine = table[table["ticker"] == ticker].sort_values("horizon")
    if mine.empty or dte is None or not np.isfinite(dte):
        return None
    hs = mine["horizon"].to_numpy(float)
    dte = float(np.clip(dte, hs[0], hs[-1]))
    j = int(np.searchsorted(hs, dte))
    lo = mine.iloc[max(j - 1, 0)]
    hi = mine.iloc[min(j, len(mine) - 1)]
    w = 0.0 if hi["horizon"] == lo["horizon"] else (dte - lo["horizon"]) / (hi["horizon"] - lo["horizon"])
    near = hi if w >= 0.5 else lo
    out = near.to_dict()
    for col in mine.columns:
        a, b = lo[col], hi[col]
        if isinstance(a, (int, float, np.floating)) and isinstance(b, (int, float, np.floating)) \
                and np.isfinite(a) and np.isfinite(b) and col not in ("horizon", "trading_days"):
            out[col] = float(a + w * (b - a))
    out["horizon"] = dte
    return out


def confidence_rank(value: str | None) -> int:
    return LEVELS.index(value) if value in LEVELS else 0


# --- Screener (D.3): annotate and filter -----------------------------------------------------

def annotate(rows: pd.DataFrame, table: pd.DataFrame, horizon: float | None = None,
             dte_column: str = "dte_calendar") -> pd.DataFrame:
    """Add `outlook_{dial}` and `outlook_{dial}_conf` to sheet rows: each row
    at its own expiry (its DTE), or every row at a fixed `horizon`."""
    out = rows.copy()
    for dial in DIALS:
        out[f"outlook_{dial}"] = np.nan
        out[f"outlook_{dial}_conf"] = None
    if table is None or table.empty or out.empty:
        return out
    cache: dict = {}
    for i, row in out.iterrows():
        dte = horizon if horizon is not None else row.get(dte_column)
        key = (str(row["ticker"]).upper(), None if dte is None else round(float(dte), 2))
        if key not in cache:
            cache[key] = at(table, key[0], key[1]) if key[1] is not None else None
        rec = cache[key]
        if not rec:
            continue
        for dial in DIALS:
            value = rec.get(dial)
            out.at[i, f"outlook_{dial}"] = value if value is not None else np.nan
            out.at[i, f"outlook_{dial}_conf"] = rec.get(f"{dial}_conf")
    return out


def filter_rows(rows: pd.DataFrame, direction: tuple | None = None, range_: tuple | None = None,
                volatility: tuple | None = None, min_confidence: str = "none",
                dte_window: tuple | None = None, dte_column: str = "dte_calendar"
                ) -> pd.DataFrame:
    """Keep annotated rows whose dials fall inside the (lo, hi) bounds given,
    each constrained dial at `min_confidence` or better, and (from-to) whose
    DTE is inside `dte_window`. e.g. direction=(6, 10) at 14d, or
    range_=(7, 10) with dte_window=(21, 45)."""
    keep = pd.Series(True, index=rows.index)
    need = confidence_rank(min_confidence)
    for dial, bounds in (("direction", direction), ("range", range_),
                         ("volatility", volatility)):
        if bounds is None:
            continue
        lo, hi = bounds
        values = rows[f"outlook_{dial}"].astype(float)
        keep &= values.between(lo, hi)
        if need:
            keep &= rows[f"outlook_{dial}_conf"].map(confidence_rank) >= need
    if dte_window is not None:
        keep &= rows[dte_column].astype(float).between(*dte_window)
    return rows[keep]
