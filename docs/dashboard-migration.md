# Dashboard Migration

ARBITER now serves a single canonical dashboard from [`arbiter/web/dashboard.html`](/Users/rentamac/Documents/arbiter/arbiter/web/dashboard.html).

The legacy prototypes `index.html` and `ARBITER_Dashboard.html` were intentionally removed because they had diverged from the package runtime, expected incompatible API shapes, and contained stale client-side behavior.

Current expectations:

- Run the backend with `python3 -m arbiter.main`.
- Use `/` for the live dashboard.
- Use `/api/system`, `/api/opportunities`, `/api/trades`, `/api/errors`, and `/ws` for live data.
- Treat the `arbiter/` package as the only backend source of truth.
