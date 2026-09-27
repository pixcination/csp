"""
Headless page check -- runs every Streamlit page and reports exceptions.

    python scripts/check_pages.py

Streamlit is a client-rendered SPA, so a `curl` against the running server
cannot see a page-level exception. `AppTest` actually executes each page's
Python. Exit code is non-zero if any page raises.

Paths are absolute: newer Streamlit resolves `AppTest.from_file` relative to
the *calling* file, so relative paths break depending on where this runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pages() -> list[Path]:
    return [ROOT / "app" / "main.py"] + sorted((ROOT / "app" / "pages").glob("*.py"))


def check(path: Path, timeout: int = 180) -> list[str]:
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(path), default_timeout=timeout)
    at.run()
    return [e.message for e in at.exception]


def main() -> int:
    failed = 0
    for path in pages():
        try:
            errors = check(path)
        except Exception as exc:
            errors = [f"{type(exc).__name__}: {exc}"]
        rel = path.relative_to(ROOT).as_posix()
        if errors:
            failed += 1
            print(f"FAIL  {rel}")
            for message in errors:
                print(f"      {message[:300]}")
        else:
            print(f"OK    {rel}")
    print(f"\n{len(pages()) - failed}/{len(pages())} pages clean")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
