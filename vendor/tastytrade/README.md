# Vendored TastyTrade client

Copied verbatim from `D:\tastytrade` by `scripts/consolidate.py` so this
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
`D:\tastytrade`, re-run `python scripts/consolidate.py --force` to refresh
the copy. `_VENDORED_FROM.txt` records where and when each file came from.
