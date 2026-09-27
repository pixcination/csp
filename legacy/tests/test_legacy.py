"""
Tests for the retired Phase-2 stack, kept runnable:  python -m pytest legacy/tests

Not part of the main suite (`pytest tests`). Moved here in Phase 8.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_missing_daily_database_reads_empty_not_raises(tmp_path, monkeypatch):
    """A fresh copy of the folder starts with an empty data/ directory. That is
    a normal state the pages already handle -- it must not be a traceback."""
    import legacy.analytics.data_access as da
    monkeypatch.setattr(da, "_daily_db_path", lambda: tmp_path / "absent.duckdb")
    frame = da.load_daily_bars("AAPL")
    assert frame.empty
    assert list(frame.columns) == da.DAILY_COLUMNS
