"""
Consolidate every external dependency into D:\\csp.

    python scripts/consolidate.py --dry-run     # show the plan, change nothing
    python scripts/consolidate.py               # do it
    python scripts/consolidate.py --verify      # check the result afterwards

WHAT THIS PROJECT ACTUALLY DEPENDS ON, AND WHAT IT COSTS TO BRING IN
--------------------------------------------------------------------
The good news is that the expensive part is already here. `data/raw_1m/`
holds full 1-minute history back to 2000 for all 61 universe tickers --
`scripts/03_copy_selected_tickers.py` has been mirroring it all along. What
remains outside is small:

    D:\\tastytrade\\tastytrade_common.py     30 KB   vendor it
    D:\\tastytrade\\snapshot_loop.py         27 KB   vendor it
    D:\\tastytrade\\reference\\*.parquet      88 KB   copy the data
    D:\\tastytrade\\.env                       -     retire (superseded)
    D:\\pricing_data\\massive_downloader.py    5 KB   already replaced

Total: under 150 KB. Consolidation is cheap because the 4-5 GB that matters
is already inside the project.

THE ONE THING NOT WORTH COPYING
-------------------------------
`D:\\pricing_data\\stocks_etfs\\1m\\` holds the full ~1,050-ticker screening
pool -- on the order of a couple hundred gigabytes. You need it for exactly
one operation: re-running Stage 1 to screen a *new* universe out of that pool.
Everything else -- scoring, backtesting, the move engine, the app -- reads
`data/raw_1m/` and never touches it.

So it becomes optional rather than required. If you later add a single ticker
to the universe, Massive can backfill its history directly into `data/raw_1m/`
without reconnecting the archive (roughly an hour for 26 years at free-tier
pacing, which is fine for an occasional addition).

After this runs, the project is self-contained: nothing outside D:\\csp is
read during normal operation, and `preflight.py` will say so.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import load_config, project_root, reference_dir  # noqa: E402

VENDOR_FILES = ["tastytrade_common.py", "snapshot_loop.py"]
REFERENCE_FILES = ["treasury_rates.parquet", "dividends.parquet",
                    "vol_indices.parquet", "_refresh.log"]

VENDOR_README = '''\
# Vendored TastyTrade client

Copied verbatim from `D:\\tastytrade` by `scripts/consolidate.py` so this
project has no runtime dependency on that folder.

**Do not import these modules directly.** Use
`data_sources.tastytrade_client`, which loads them through this package and
calls `core.env.bind_tastytrade_client()` first. That call repoints the
client's CWD-relative `ENV_PATH = Path(".env")` at the project's single
authoritative `.env` before any OAuth refresh can fire -- which is the fix
for the credential drift that put two different refresh tokens on this disk.

Importing `tastytrade_common` yourself skips that binding, and the rotating
token will start going wherever your shell happened to be standing.

## Updating

These are a snapshot, not a live link. If you improve the client in
`D:\\tastytrade`, re-run `python scripts/consolidate.py --force` to refresh
the copy. `_VENDORED_FROM.txt` records where and when each file came from.
'''


class Plan:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.actions: list[tuple[str, str]] = []
        self.problems: list[str] = []

    def note(self, verb: str, detail: str) -> None:
        self.actions.append((verb, detail))
        prefix = "would " if self.dry_run else ""
        print(f"  [{verb:<6}] {prefix}{detail}")

    def problem(self, detail: str) -> None:
        self.problems.append(detail)
        print(f"  [ISSUE ] {detail}")


def _copy(src: Path, dst: Path, plan: Plan, force: bool) -> bool:
    if not src.exists():
        plan.problem(f"source missing: {src}")
        return False
    if dst.exists() and not force:
        if dst.stat().st_size == src.stat().st_size:
            plan.note("skip", f"{dst.name} already present and same size")
            return True
        plan.note("skip", f"{dst.name} exists with a different size "
                          f"-- re-run with --force to overwrite")
        return True
    if not plan.dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    plan.note("copy", f"{src}  ->  {dst.relative_to(project_root())}")
    return True


def vendor_tastytrade(plan: Plan, force: bool) -> None:
    print("\n1. Vendor the TastyTrade client")
    cfg = load_config()
    source_dir = Path(cfg.get("tastytrade_pipeline_dir", "D:/tastytrade"))
    target = project_root() / "vendor" / "tastytrade"

    if not source_dir.is_dir():
        plan.problem(f"{source_dir} not found -- cannot vendor the client. "
                     f"If you have already consolidated, this is expected.")
        return

    ok = True
    for name in VENDOR_FILES:
        ok &= _copy(source_dir / name, target / name, plan, force)

    if ok and not plan.dry_run:
        (target / "__init__.py").write_text(
            '"""Vendored TastyTrade client. Import via '
            'data_sources.tastytrade_client, never directly."""\n',
            encoding="utf-8")
        (target / "README.md").write_text(VENDOR_README, encoding="utf-8")
        import datetime as dt
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        (target / "_VENDORED_FROM.txt").write_text(
            f"source: {source_dir}\ncopied: {stamp}\n"
            + "".join(f"  {n}\n" for n in VENDOR_FILES), encoding="utf-8")
        plan.note("write", "vendor/tastytrade/{__init__.py, README.md, _VENDORED_FROM.txt}")


def copy_reference_data(plan: Plan, force: bool) -> None:
    print("\n2. Copy reference data (Treasury rates, VIX complex, dividends)")
    cfg = load_config()
    source = Path(cfg.get("tastytrade_pipeline_dir", "D:/tastytrade")) / "reference"
    target = reference_dir()
    if not source.is_dir():
        plan.problem(f"{source} not found -- the reference refresh will "
                     f"rebuild these from FRED and Yahoo on first run.")
        return
    for name in REFERENCE_FILES:
        src = source / name
        if src.exists():
            _copy(src, target / name, plan, force)
        else:
            plan.note("skip", f"{name} not present at source")


def check_intraday_archive(plan: Plan) -> None:
    print("\n3. Verify the 1-minute archive is already self-contained")
    from core.paths import load_universe, raw_1m_dir
    universe = load_universe()
    local = raw_1m_dir()
    if not universe:
        plan.problem("output/final_universe.txt is empty -- cannot verify coverage")
        return

    missing, thin, total_files, total_bytes = [], [], 0, 0
    for ticker in universe:
        folder = local / ticker
        if not folder.is_dir():
            missing.append(ticker)
            continue
        files = list(folder.glob(f"{ticker}_*_1m.txt"))
        total_files += len(files)
        total_bytes += sum(f.stat().st_size for f in files)
        if len(files) < 12:
            thin.append(f"{ticker} ({len(files)} months)")

    have = len(universe) - len(missing)
    plan.note("check", f"{have}/{len(universe)} universe tickers present in "
                       f"data/raw_1m ({total_files:,} files, "
                       f"{total_bytes / 1e9:.1f} GB)")
    if missing:
        plan.problem(f"no local history for: {', '.join(missing[:10])}"
                     + (" ..." if len(missing) > 10 else "")
                     + "  -- run scripts/03_copy_selected_tickers.py before "
                       "disconnecting D:\\pricing_data")
    if thin:
        plan.note("warn", f"thin history: {', '.join(thin[:6])}"
                          + (" ..." if len(thin) > 6 else ""))


def rewrite_config(plan: Plan) -> None:
    print("\n4. Rewrite config paths to point inside the project")
    path = project_root() / "config.yaml"
    text = path.read_text(encoding="utf-8")

    replacements = [
        ('tastytrade_pipeline_dir: "D:/tastytrade"',
         '# Vendored into vendor/tastytrade by scripts/consolidate.py. This key\n'
         '# is retained only so the legacy scripts/04 still resolves; nothing\n'
         '# in the new pipeline reads it.\n'
         'tastytrade_pipeline_dir: "vendor/tastytrade"'),
    ]
    changed = False
    for old, new in replacements:
        if old in text:
            text = text.replace(old, new)
            changed = True
            plan.note("edit", "config.yaml: tastytrade_pipeline_dir -> vendor/tastytrade")

    if "pricing_data_required:" not in text:
        marker = 'pricing_data_root:'
        idx = text.find(marker)
        if idx >= 0:
            line_end = text.find("\n", idx) + 1
            insert = (
                "\n# The full ~1,050-ticker screening pool. NOT required for normal\n"
                "# operation -- everything reads data/raw_1m/ instead. Needed only to\n"
                "# re-run Stage 1 against the whole pool. Set to false once you have\n"
                "# disconnected the drive; preflight will stop warning about it.\n"
                "pricing_data_required: false\n")
            text = text[:line_end] + insert + text[line_end:]
            changed = True
            plan.note("edit", "config.yaml: added pricing_data_required: false")

    if changed and not plan.dry_run:
        backup = path.with_suffix(".yaml.bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(text, encoding="utf-8")
        plan.note("write", f"config.yaml updated (backup at {backup.name})")
    elif not changed:
        plan.note("skip", "config.yaml already consolidated")


def retire_stray_env(plan: Plan) -> None:
    print("\n5. Retire the duplicate .env")
    from core import env
    strays = env.stray_env_files()
    if not strays:
        plan.note("check", "no duplicate .env found")
        return
    for stray in strays:
        target = stray.with_suffix(".env.retired") if stray.suffix else \
            stray.with_name(".env.retired")
        if target.exists():
            plan.note("skip", f"{target} already exists")
            continue
        if not plan.dry_run:
            try:
                stray.rename(target)
                plan.note("rename", f"{stray} -> {target.name}")
            except OSError as exc:
                plan.problem(f"could not rename {stray}: {exc}")
        else:
            plan.note("rename", f"{stray} -> {target.name}")


def verify() -> int:
    """Confirm nothing outside the project is needed to start up."""
    print("=" * 72)
    print("  Consolidation check")
    print("=" * 72)
    root = project_root()
    problems = []

    vendor = root / "vendor" / "tastytrade"
    for name in VENDOR_FILES:
        present = (vendor / name).exists()
        print(f"  [{'OK  ' if present else 'MISS'}] vendor/tastytrade/{name}")
        if not present:
            problems.append(name)

    for name in REFERENCE_FILES[:3]:
        present = (reference_dir() / name).exists()
        print(f"  [{'OK  ' if present else 'WARN'}] data/reference/{name}"
              + ("" if present else "   (rebuilt on first reference refresh)"))

    try:
        import importlib
        sys.path.insert(0, str(root))
        importlib.import_module("data_sources.tastytrade_client")
        print("  [OK  ] data_sources.tastytrade_client imports the vendored copy")
    except Exception as exc:
        print(f"  [FAIL] client import failed: {type(exc).__name__}: {exc}")
        problems.append("client import")

    cfg = load_config()
    external = Path(str(cfg.get("pricing_data_root", "")))
    if cfg.get("pricing_data_required", True):
        print(f"  [WARN] pricing_data_required is still true ({external})")
    else:
        print(f"  [OK  ] pricing_data marked optional -- only needed to re-screen "
              f"a new universe")

    print()
    if problems:
        print(f"  {len(problems)} item(s) still outstanding: {', '.join(problems)}")
        return 1
    print("  Self-contained. Nothing outside D:\\csp is read at startup.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    ap.add_argument("--force", action="store_true", help="overwrite files that already exist")
    ap.add_argument("--verify", action="store_true", help="check a completed consolidation")
    ap.add_argument("--keep-stray-env", action="store_true",
                     help="do not rename D:\\tastytrade\\.env")
    args = ap.parse_args()

    if args.verify:
        return verify()

    print("=" * 72)
    print("  Consolidating external dependencies into " + str(project_root()))
    if args.dry_run:
        print("  DRY RUN -- nothing will be written")
    print("=" * 72)

    plan = Plan(args.dry_run)
    vendor_tastytrade(plan, args.force)
    copy_reference_data(plan, args.force)
    check_intraday_archive(plan)
    rewrite_config(plan)
    if not args.keep_stray_env:
        retire_stray_env(plan)

    print("\n" + "=" * 72)
    print(f"  {len(plan.actions)} action(s), {len(plan.problems)} issue(s)")
    if plan.problems:
        for item in plan.problems:
            print(f"    - {item}")
    if args.dry_run:
        print("\n  Re-run without --dry-run to apply.")
    else:
        print("\n  Next: python scripts/consolidate.py --verify")
        print("        python scripts/preflight.py")
    return 1 if plan.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
