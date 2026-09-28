"""Phase 20: the Outlook walk-forward (skill per symbol and horizon) and the
final pooled fit, then the live dials.

    .venv\Scripts\python scripts\validate_outlook.py

Writes data/validation/outlook_skill.parquet, data/outlook/model.json and
data/outlook/latest.parquet. The pipeline does the same when the model is
older than config.yaml -> outlook.refit_days.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import outlook  # noqa: E402
from core.paths import load_universe  # noqa: E402
from core.progress import ConsoleReporter  # noqa: E402

if __name__ == "__main__":
    symbols = load_universe(scope="all")
    reporter = ConsoleReporter()
    result = outlook.validate(symbols, reporter=reporter)
    skill = result["skill"]
    pooled = skill[(skill["symbol"] == "ALL") & (skill["component"] == "blend")]
    print(pooled.pivot_table(index="horizon", columns="event", values="bss").round(4))
    frame = outlook.build(symbols, reporter=reporter)
    print(f"live Outlook: {frame['ticker'].nunique()} symbols x {frame['horizon'].nunique()} horizons")
