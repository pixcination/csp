"""Phase 18: run and chain retention (pipeline/retention.py). Prints the plan;
--apply deletes. Manifests, referenced runs and data/chain_archive are kept.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import retention  # noqa: E402

if __name__ == "__main__":
    planned = retention.plan()
    print(f"{len(planned['run_files'])} run table(s), {len(planned['chain_blocks'])} chain "
          f"block(s), {planned['bytes'] / 1e6:.1f} MB; runs kept because a position references "
          f"them: {len(planned['kept_referenced_runs'])}")
    if "--apply" in sys.argv:
        print(retention.apply(planned))
    else:
        print("dry run -- pass --apply to delete")
