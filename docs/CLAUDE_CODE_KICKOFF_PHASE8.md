# Claude Code kickoff — Phase 8 onward (CSP/PCS screener)

Paste this as your first message to Claude Code, run from a terminal opened at
`D:\csp`.

---

Read `docs/SCREENER_ROADMAP.md` in full. It contains a gap analysis of this
codebase against the target product (a CSP + put-credit-spread screener that
grows into a multi-strategy options screener), the design decisions behind
it, and phased work orders (Phase 8 through 16+).

Then read `docs/ARCHITECTURE.md`, `docs/PHASE1_NOTES.md`, and the module
docstrings in `analytics/`, `data_sources/`, `core/` and `pipeline/run.py`.
The docstrings record why Phases 1–7 made the choices they did; don't reverse
one without asking me.

Start with **Phase 8 (§C.0)** only:

1. Run preflight and the test suite and tell me the baseline.
2. Propose your plan for Phase 8 and list the "decisions to confirm" for it.
   Wait for my answers before writing code.
3. Follow the ground rules in Part C of the roadmap: config-driven, paths via
   `core.paths`, no Streamlit in `analytics/`, real data for validation,
   `tests/test_phase8.py`, headless page checks, `docs/PHASE8_SUMMARY.md`,
   ARCHITECTURE.md updated, commit and push at the end.

This stays a research and analysis tool: no order placement.
