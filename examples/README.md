# examples/

Things an adopter copies, not things the platform runs. `TO-BE.md` §12 ships
capabilities and no instruments, which leaves a gap where a component would have
been — this directory is what fills it: a worked example, in a form you can point
your own tooling at, that nothing here imports.

Everything in it is tested anyway. An example that has never been run is a guess.

- **`dashboards/`** — the `ARCHITECTURE.md` §9 metric families as panels over the
  `reporting` views: `reporting.sql` for any Postgres driver, `grafana-reporting.json`
  for one that reads dashboards. `OBSERVABILITY.md` explains the read boundary they
  stay inside, and `tests/test_dashboards.py` holds them to it.
