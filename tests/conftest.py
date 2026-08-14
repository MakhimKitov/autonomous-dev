"""Test-wide defaults.

`open_ledger` honours `LEDGER_DSN`, which means that once the variable is set —
as it is for the whole Postgres CI job — *every* `PlatformStores.open(data_dir)`
stops using `data_dir` for its ledger and goes to the service database instead.
That is correct in production (the DSN *is* the ledger, and `--data-dir` never
had a say) and wrong for a test suite, where it turns one shared database into an
order-dependent fixture for tests that only ever wanted a scratch ledger.

So: by default a test gets the single-node backend, isolated under its own
tmp_path. The two files that mean to exercise Postgres read `LEDGER_DSN` at
import time into a module constant and pass it explicitly, so they are unaffected
by this and still run — which is what lets one `pytest` invocation cover both
backends without either interfering with the other.

Full-scenario parity across backends is gate A7's rehearsal (#18), not this
suite's job: the scheduler tests fake GitHub and docker, and giving each of them
its own database would cost a CREATE DATABASE per test to prove something the
ledger contract already proves per method.
"""

import pytest
from control_plane.store import LEDGER_DSN_ENV


@pytest.fixture(autouse=True)
def isolated_ledger_by_default(monkeypatch):
    monkeypatch.delenv(LEDGER_DSN_ENV, raising=False)
