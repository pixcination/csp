"""
Reference data: Treasury rates and the VIX complex.

Replaces `D:\\tastytrade\\data_refresh.py`, which has been faithfully
downloading this data for months while nothing in the wheel tool read any of
it. `config.yaml` meanwhile hardcodes `risk_free_rate: 0.045`.

Two free wins live here:

* **A live risk-free rate.** Every Black-Scholes call in the project uses a
  static 4.5%. At 7 DTE the rate barely moves an option price, so this is not
  urgent -- but it costs one function to stop guessing, and the same series
  is the right discount rate for annualised-return comparisons, where it
  matters considerably more.

* **The VIX term structure**, which `analytics/regime.py` turns into a
  position-sizing gate. VIX9D over VIX3M is the single most useful regime
  signal available to a short-premium book, and it has been sitting in a
  parquet file on your disk unused.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from core import env
from core.paths import load_config, reference_dir
from core.progress import BaseReporter, NullReporter

RATES_FILE = "treasury_rates.parquet"
VOL_FILE = "vol_indices.parquet"

FRED_SERIES = ["DGS1MO", "DGS3MO", "DGS6MO", "DGS1"]
VOL_TICKERS = {"^VIX": "VIX", "^VIX9D": "VIX9D", "^VIX3M": "VIX3M",
                "^VVIX": "VVIX", "^SKEW": "SKEW"}

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


@dataclass
class RefreshResult:
    source: str
    rows: int = 0
    ok: bool = False
    skipped: bool = False
    error: str | None = None
    note: str = ""


# --- Treasury rates --------------------------------------------------------

def refresh_rates(start: str = "2015-01-01",
                   reporter: BaseReporter | None = None) -> RefreshResult:
    result = RefreshResult(source="FRED treasury rates")
    key = env.get("FRED_API_KEY")
    if not key:
        result.skipped = True
        result.note = ("FRED_API_KEY not set -- keeping the static rate from "
                       "config.yaml. This is a minor approximation at 7 DTE.")
        return result

    import requests
    frames = []
    try:
        for series in FRED_SERIES:
            response = requests.get(FRED_URL, timeout=30, params={
                "series_id": series, "api_key": key, "file_type": "json",
                "observation_start": start,
            })
            response.raise_for_status()
            observations = response.json().get("observations", [])
            frame = pd.DataFrame(observations)[["date", "value"]]
            frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
            frame = frame.rename(columns={"value": series})
            frames.append(frame.set_index("date"))
        merged = pd.concat(frames, axis=1).reset_index()
        merged.to_parquet(reference_dir() / RATES_FILE, index=False)
        result.ok, result.rows = True, len(merged)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {str(exc)[:150]}"
    return result


def risk_free_rate(dte: int = 7) -> tuple[float, str]:
    """Annualised rate for the tenor nearest `dte`, with its provenance.

    Falls back to the configured static rate, and says which was used --
    a silent fallback is how a stale number becomes an unexamined assumption.
    """
    static = float(load_config().get("analytics", {}).get("risk_free_rate", 0.045))
    path = reference_dir() / RATES_FILE
    if not path.exists():
        return static, f"static {static:.2%} from config (no FRED data on disk)"
    try:
        frame = pd.read_parquet(path)
    except Exception:
        return static, f"static {static:.2%} from config (rates file unreadable)"
    if frame.empty:
        return static, f"static {static:.2%} from config (rates file empty)"

    series = ("DGS1MO" if dte <= 45 else "DGS3MO" if dte <= 135
              else "DGS6MO" if dte <= 270 else "DGS1")
    if series not in frame.columns:
        return static, f"static {static:.2%} from config ({series} missing)"
    values = frame[["date", series]].dropna()
    if values.empty:
        return static, f"static {static:.2%} from config ({series} all null)"
    row = values.iloc[-1]
    rate = float(row[series]) / 100.0
    return rate, f"{series} {rate:.2%} as of {row['date']}"


# --- Volatility indices ----------------------------------------------------

def refresh_vol_indices(period: str = "5y",
                         reporter: BaseReporter | None = None) -> RefreshResult:
    result = RefreshResult(source="VIX complex")
    try:
        import yfinance as yf
    except ImportError:
        result.error = "yfinance not installed"
        return result

    frames = []
    try:
        for symbol, column in VOL_TICKERS.items():
            history = yf.Ticker(symbol).history(period=period)
            if history is None or history.empty:
                continue
            series = history["Close"].rename(column)
            series.index = pd.to_datetime(series.index).tz_localize(None)
            frames.append(series)
        if not frames:
            result.error = "no volatility index data returned"
            return result
        merged = pd.concat(frames, axis=1).reset_index()
        merged = merged.rename(columns={merged.columns[0]: "date"})
        merged.to_parquet(reference_dir() / VOL_FILE, index=False)
        result.ok, result.rows = True, len(merged)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {str(exc)[:150]}"
    return result


# --- Combined --------------------------------------------------------------

def refresh_all(reporter: BaseReporter | None = None,
                 max_age_hours: int = 20) -> list[RefreshResult]:
    """Refresh rates and volatility indices if they are stale.

    Both are daily series, so re-pulling more than once a day is wasted work;
    the age check keeps this stage near-free on repeat runs.
    """
    reporter = reporter or NullReporter()
    results = []
    with reporter.stage("reference", "Reference data", total=2):
        for label, path, fn in (
                ("treasury rates", reference_dir() / RATES_FILE, refresh_rates),
                ("volatility indices", reference_dir() / VOL_FILE, refresh_vol_indices)):
            if path.exists():
                age = dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)
                if age < dt.timedelta(hours=max_age_hours):
                    res = RefreshResult(source=label, skipped=True, ok=True,
                                         note=f"{age.total_seconds() / 3600:.0f}h old")
                    results.append(res)
                    reporter.advance(1, note=f"{label} current")
                    continue
            res = fn(reporter=reporter)
            results.append(res)
            reporter.advance(1, note=f"{label} "
                                     + ("ok" if res.ok else res.error or "skipped"))
    return results
