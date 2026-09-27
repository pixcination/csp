"""
Preflight -- run this first, and whenever something behaves oddly.

    python scripts/preflight.py

Checks, in the order that things actually break:

  1. Python version and the virtual environment
  2. Dependencies
  3. Credentials, and whether a second .env is lurking (finding F-01)
  4. Market calendar backend (finding F-12)
  5. Data freshness: 1-minute archive, chain snapshots, reference data
  6. Account capacity against the configured universe

Exits 0 if everything required is present, 1 otherwise, so it can gate a
scheduled run.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Module-relative bootstrap: this script must work from any working directory,
# which is the whole point of the exercise.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import env  # noqa: E402
from core.paths import (config_path, data_dir, load_config, load_universe,  # noqa: E402
                         project_root, reference_dir)

REQUIRED_PACKAGES = [
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("duckdb", "duckdb"),
    ("yaml", "pyyaml"),
    ("scipy", "scipy"),
    ("dotenv", "python-dotenv"),
    ("requests", "requests"),
    ("websockets", "websockets"),
    ("pyarrow", "pyarrow"),
    ("yfinance", "yfinance"),
    ("streamlit", "streamlit"),
    ("plotly", "plotly"),
    ("pandas_market_calendars", "pandas_market_calendars"),
]
OPTIONAL_PACKAGES = [
    ("massive", "massive-api  (historical 1-minute archive)"),
    ("openpyxl", "openpyxl     (Excel export from move analysis)"),
]

OK, WARN, FAIL = "  OK  ", " WARN ", " FAIL "
_failures: list[str] = []
_warnings: list[str] = []


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def check(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}" + (f"  {detail}" if detail else ""))
    if status is FAIL:
        _failures.append(label)
    elif status is WARN:
        _warnings.append(label)


def check_python() -> None:
    section("1. Interpreter")
    version = ".".join(str(v) for v in sys.version_info[:3])
    check(OK if sys.version_info >= (3, 11) else FAIL,
          f"Python {version}",
          "" if sys.version_info >= (3, 11) else "3.11 or newer required")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    expected = project_root() / ".venv"
    if in_venv:
        check(OK, "Virtual environment active", sys.prefix)
    else:
        check(WARN, "No virtual environment",
              f"Global interpreter is shared with your other projects. "
              f"Create one with: python -m venv {expected}")
    check(OK, "Project root", str(project_root()))
    check(OK if config_path().exists() else FAIL, "config.yaml", str(config_path()))


def check_packages() -> None:
    section("2. Dependencies")
    import importlib.util
    missing = []
    for module, package in REQUIRED_PACKAGES:
        if importlib.util.find_spec(module) is None:
            missing.append(package)
    if missing:
        check(FAIL, f"{len(missing)} required package(s) missing",
              "pip install " + " ".join(missing))
    else:
        check(OK, f"All {len(REQUIRED_PACKAGES)} required packages present")
    for module, description in OPTIONAL_PACKAGES:
        if importlib.util.find_spec(module) is None:
            check(WARN, f"Optional: {module} not installed", description)


def check_credentials() -> None:
    section("3. Credentials")
    healthy, report = env.doctor()
    for line in report.splitlines():
        if line.strip():
            print("       " + line)
    check(OK if healthy else FAIL, "Required credentials",
          "" if healthy else "see above")
    strays = env.stray_env_files()
    for stray in strays:
        check(WARN, "A second .env exists", str(stray))
    if strays:
        print("       This file is no longer read, but its mere existence is what")
        print("       caused the rotating TastyTrade refresh token to drift between")
        print("       folders. Rename it to .env.retired once you have confirmed")
        print(f"       {env.env_file()} holds the live credentials.")


def check_calendar() -> None:
    section("4. Market calendar")
    from core.market_calendar import calendar_available, classify, session_block
    if calendar_available():
        check(OK, "pandas_market_calendars (NYSE)", "holidays and early closes for any year")
    else:
        check(WARN, "Degraded calendar",
              "weekday-only approximation; market holidays will be treated as "
              "trading days. Install pandas_market_calendars.")
    info = classify()
    severity, message = info.banner()
    check(OK, f"Current session: {info.state.value}", f"block {session_block()}")
    print(f"       {message}")


def check_data_freshness() -> None:
    section("5. Data freshness")
    universe = load_universe()
    if not universe:
        check(FAIL, "Universe list empty", "output/final_universe.txt not found or blank")
        return
    check(OK, f"Universe: {len(universe)} tickers")

    try:
        from data_sources.massive_sync import archive_status
        rows = archive_status(universe)
        absent = [r for r in rows if r["last_bar"] is None]
        behind = [r for r in rows if r["last_bar"] and not r["current"]]
        gaps = [r["days_behind"] for r in rows if r["days_behind"] is not None]
        worst = max(gaps) if gaps else None

        if absent and len(absent) == len(rows):
            from data_sources.massive_sync import archive_root
            check(FAIL, "1-minute archive not found",
                  f"no data under {archive_root()} for any of {len(rows)} tickers "
                  f"-- run scripts/03_copy_selected_tickers.py with the external "
                  f"archive connected, or sync from Massive")
        elif absent:
            check(FAIL, f"1-minute archive missing for {len(absent)} ticker(s)",
                  ", ".join(sorted(r["ticker"] for r in absent)[:10]))
        elif not behind:
            check(OK, "1-minute archive current")
        elif worst and worst > 30:
            check(FAIL, f"1-minute archive {worst} days behind",
                  f"{len(behind)}/{len(rows)} tickers stale. "
                  f"Run: python scripts/sync_archive.py")
        else:
            check(WARN, f"1-minute archive {worst} days behind",
                  f"{len(behind)}/{len(rows)} tickers stale")
    except Exception as exc:
        check(WARN, "Could not read 1-minute archive", str(exc)[:120])

    try:
        from data_sources.yfinance_sync import daily_data_status
        status = daily_data_status()
        if status["source"] == "daily_bars_tr":
            check(OK, "Daily bars (total return)",
                  f"{status['total_return_rows']:,} rows / "
                  f"{status['total_return_tickers']} tickers")
        elif status["source"] == "daily_bars":
            check(WARN, "Daily bars: legacy table only",
                  f"{status['legacy_rows']:,} rows, split-adjusted. "
                  f"Run pipeline/run.py to build total-return bars.")
        else:
            check(FAIL, "No daily price data", status["action"] or "")
    except Exception as exc:
        check(WARN, "Could not inspect daily bars", str(exc)[:100])

    for label, path in [
        ("1-minute cache", data_dir() / "raw_1m_cache.duckdb"),
        ("IV history", data_dir() / "iv_history.duckdb"),
        ("Wheel ledger", data_dir() / "trade_log.duckdb"),
    ]:
        check(OK if path.exists() else WARN, label,
              f"{path.stat().st_size / 1e6:.0f} MB" if path.exists() else "not built yet")

    for label, name in [("Treasury rates", "treasury_rates.parquet"),
                         ("Volatility indices", "vol_indices.parquet"),
                         ("Earnings calendar", "earnings.parquet"),
                         ("Dividends", "dividends.parquet")]:
        path = reference_dir() / name
        check(OK if path.exists() else WARN, label,
              "" if path.exists() else "run the reference refresh")


def check_portability() -> None:
    section("6. Portability")
    from core.paths import (is_portable, pricing_data_required, resolve,
                             tastytrade_pipeline_dir)
    portable, blockers = is_portable()
    if portable:
        check(OK, "Self-contained",
              "nothing outside the project folder is required to start")
    else:
        for blocker in blockers:
            check(FAIL, "Portability blocker", blocker)
    # Report what is actually on disk, not merely that a path could be formed.
    # This line used to print OK unconditionally, so it contradicted the
    # blocker two lines above it and made a missing vendor/ look fine.
    client_dir = tastytrade_pipeline_dir()
    if (client_dir / "tastytrade_common.py").exists():
        check(OK, "TastyTrade client", str(client_dir))
    else:
        check(FAIL, "TastyTrade client missing",
              f"{client_dir} -- run scripts/consolidate.py to vendor it")

    archive = resolve(load_config().get("pricing_data_root"))
    if pricing_data_required():
        check(WARN, "External archive is marked required", str(archive))
    elif archive and archive.is_dir():
        check(OK, "External archive present but optional", str(archive))
    else:
        check(OK, "External archive absent and not required",
              "only needed to re-screen a new universe from the full pool")


def check_capacity() -> None:
    section("7. Account capacity")
    from analytics import sizing
    report = sizing.capacity_report()
    cfg = load_config().get("account", {})
    check(OK, f"{cfg.get('account_type', 'account')} "
              f"${report['net_liquidating_value']:,.0f}",
          f"cash-secured: {cfg.get('require_cash_secured', True)}")
    print(f"       deployable after buffer : ${report['deployable_cash']:,.0f}")
    print(f"       max collateral/position : ${report['typical_position_size']:,.0f}")
    print(f"       highest tradable strike : ${report['max_tradable_strike']:,.0f}")
    print(f"       max open positions      : {report['max_open_positions']}")

    universe = load_universe()
    tags_path = project_root() / "output" / "stage2_quality_tags_master.csv"
    if universe and tags_path.exists():
        import csv
        tags = {r["ticker"]: r for r in csv.DictReader(open(tags_path, encoding="utf-8"))}
        rows = [tags[t] for t in universe if t in tags and tags[t].get("last_price")]
        tradable, blocked = sizing.screen_universe_by_price(rows)
        pct = len(tradable) / len(rows) if rows else 0
        status = OK if pct > 0.4 else WARN
        check(status, f"{len(tradable)}/{len(rows)} universe names fit the account",
              f"{len(blocked)} need more collateral than the position cap allows")
        if blocked:
            names = ", ".join(sorted(b["ticker"] for b in blocked)[:12])
            print(f"       blocked on price: {names}"
                  + (" ..." if len(blocked) > 12 else ""))


def main() -> int:
    print("=" * 72)
    print("  Wheel engine preflight")
    print("=" * 72)
    for fn in (check_python, check_packages, check_credentials,
                check_calendar, check_data_freshness, check_portability,
                check_capacity):
        try:
            fn()
        except Exception as exc:  # a broken check must not hide the others
            check(FAIL, f"{fn.__name__} crashed", f"{type(exc).__name__}: {exc}")

    section("Summary")
    if _failures:
        print(f"[{FAIL}] {len(_failures)} blocking issue(s):")
        for item in _failures:
            print(f"         - {item}")
    if _warnings:
        print(f"[{WARN}] {len(_warnings)} warning(s):")
        for item in _warnings:
            print(f"         - {item}")
    if not _failures and not _warnings:
        print(f"[{OK}] Everything checks out.")
    elif not _failures:
        print(f"[{OK}] No blocking issues -- safe to run.")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
