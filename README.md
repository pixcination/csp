# CSP Wheel Analysis Tool

A local research/analysis tool for selecting cash-secured put (and covered
call) candidates for a wheel strategy — screening pipeline, analytics
engine, and a Streamlit app. Not a trading tool: no order placement, no
broker execution.

## Quick start

```powershell
cd D:\csp
python launch.py
```

## Documentation

All documentation lives in **[`docs/`](docs/)**:

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — the complete system
  documentation: repository layout, data formats, the analytics engine's
  formulas and modules, the app's pages, `config.yaml` reference, and
  step-by-step instructions to reproduce this project from scratch. Start
  here.
- **[docs/README.md](docs/README.md)** — Phase 0: the universe-narrowing
  screening pipeline (`scripts/01`-`05`) that produces the tradable
  candidate list the app scores and displays.
- **[docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md)** — the original project
  brief/spec this tool was built against.
- **[docs/CLAUDE_CODE_KICKOFF.md](docs/CLAUDE_CODE_KICKOFF.md)** — the
  kickoff prompt used to hand the application-build phase to Claude Code.
