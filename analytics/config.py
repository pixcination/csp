"""
Compatibility shim over `core.paths`.

This module used to be a second, independent config loader. It resolved the
project root by *reading it out of config.yaml* -- `project_root: "D:/csp"` --
which meant the folder announced its own absolute location and could not be
moved. Worse, `Path("D:/csp")` is not absolute on Linux or macOS, so on any
other machine it silently became a relative path and every database opened
somewhere under the current working directory. The Trade Log page failed
exactly this way: `.../build/D:/csp/data/trade_log.duckdb`.

It also kept its own `lru_cache` over the same file as `core.paths`, so
`reload_config()` on one loader left the other holding stale values.

Both problems are gone: everything here now delegates. `core.paths` derives the
root from this file's own location (overridable via WHEEL_PROJECT_ROOT), so the
folder works wherever it is put -- which is the portability requirement.
Existing imports keep working unchanged.
"""
from pathlib import Path

from core.paths import load_config, project_root, reload_config, resolve

__all__ = ["load_config", "project_root", "reload_config", "resolve", "Path"]
