"""The Postgres ledger — the same nine-method protocol, on a service database.

`SqliteLedger` and this class are one design with and without contention, not two
modes (STORES.md). Callers can't tell which they hold, and that is the property
gate A7 measures: the MVP scenario runs byte-for-byte on either backend.

Three things make them behave identically while the database gets real types:

**Timestamps convert at the edge.** Columns are `timestamptz`, so the database can
index them and do `claimed_until < now()` arithmetic in SQL; the Python vocabulary
stays ISO-8601 UTC strings, exactly as the dataclasses have always held them.
Conversion happens here and nowhere else — on bind and on load — so no caller
learns which backend it's on, and application code still does no time math at all.
Loads normalize through `astimezone(UTC)`, so a session timezone cannot leak into
a returned string.

**`numeric` money becomes `float` on the way out.** `cost_usd` is `numeric` in the
schema, which is right for money and wrong for `BurstRecord.cost_usd: float | None`
— psycopg would hand back `Decimal`. It converts at the same edge.

**Nothing holds a transaction open.** The connection is `autocommit`, every write
is one statement — including `record_violation`, which writes two rows through one
data-modifying CTE rather than two statements in a transaction. The only
compare-and-set is `finish_burst`'s. There is no method here that could keep a
transaction open across application logic or I/O (STORES.md, "transactional, but
not interactive"), and no ORM to accidentally introduce one.

Deliberately still absent: the lease/claim statements, which need `claimed_by` /
`claimed_until` columns and land with E1 (#32) where contention is real.

Connection handling is one long-lived connection with a reconnect guard — enough
for a single scheduler, and the seam a pool replaces when replicas arrive (E).
"""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from control_plane.models import (
    BURST_FINALIZE_COLUMNS,
    EVENT_CHANNEL,
    EVENT_VIOLATION_RECORDED,
    BurstRecord,
    Event,
    TaskRecord,
    Violation,
    utc_now,
)

log = structlog.get_logger()

# Which columns are timestamps, per table. Declared, not detected: the store layer
# is the only place that knows, and a silent miss would hand a caller a datetime
# where every other backend hands it a string.
_TASK_TIMESTAMPS = ("last_burst_at", "created_at", "updated_at")
_BURST_TIMESTAMPS = ("started_at", "finished_at")
_VIOLATION_TIMESTAMPS = ("at",)
_EVENT_TIMESTAMPS = ("created_at",)

# numeric → float on load; see the module docstring.
_BURST_NUMERICS = ("cost_usd",)

# What finish_burst writes: BURST_FINALIZE_COLUMNS in models.py, shared with the
# SQLite backend so the two CAS statements cannot drift apart.


def _to_datetime(value: str | None) -> datetime | None:
    """ISO string → aware datetime, for binding. Naive input is read as UTC."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _to_iso(value: datetime | None) -> str | None:
    """Aware datetime → the ISO-8601 UTC string the dataclasses speak."""
    return None if value is None else value.astimezone(UTC).isoformat()


def _bind(record: Any, timestamps: tuple[str, ...]) -> dict[str, Any]:
    data = asdict(record)
    for column in timestamps:
        data[column] = _to_datetime(data[column])
    return data


def _load(
    row: dict[str, Any],
    cls: type,
    timestamps: tuple[str, ...],
    numerics: tuple[str, ...] = (),
) -> Any:
    data = dict(row)
    for column in timestamps:
        data[column] = _to_iso(data[column])
    for column in numerics:
        if data.get(column) is not None:
            data[column] = float(data[column])
    return cls(**data)


class PostgresLedger:
    """The service-database ledger. Same protocol `SqliteLedger` implements."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn = self._connect()

    def _connect(self) -> psycopg.Connection:
        # autocommit: every write below is a single statement, so a transaction
        # block would only widen the window a lock is held for.
        return psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)

    def _execute(self, sql: str, params: Any = None) -> psycopg.Cursor:
        """Run one statement, reconnecting once if the connection went away.

        A scheduler outlives database restarts and idle timeouts; a dropped
        connection should cost a reconnect, not the loop.
        """
        try:
            return self._conn.execute(sql, params)
        except psycopg.OperationalError as exc:
            log.warning("ledger connection lost — reconnecting", error=str(exc))
            try:
                self._conn.close()
            except psycopg.Error:  # already broken; the close is best-effort
                pass
            self._conn = self._connect()
            return self._conn.execute(sql, params)

    # --- tasks ---

    def upsert_task(self, task: TaskRecord) -> None:
        task.updated_at = utc_now()
        data = _bind(task, _TASK_TIMESTAMPS)
        columns = ", ".join(data)
        placeholders = ", ".join(f"%({column})s" for column in data)
        updates = ", ".join(
            f"{column} = EXCLUDED.{column}"
            for column in data
            if column not in ("task_id", "created_at")
        )
        self._execute(
            f"INSERT INTO tasks ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (task_id) DO UPDATE SET {updates}",
            data,
        )

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._execute("SELECT * FROM tasks WHERE task_id = %s", (task_id,)).fetchone()
        return None if row is None else _load(row, TaskRecord, _TASK_TIMESTAMPS)

    def tasks_by_status(self, *statuses: str) -> list[TaskRecord]:
        # ORDER BY created_at alone is not a total order, and an unbroken tie is
        # resolved by the plan — index order under one, heap order under another.
        # `task_id` makes the answer the query's rather than the planner's (A7).
        rows = self._execute(
            "SELECT * FROM tasks WHERE status = ANY(%s) ORDER BY created_at, task_id",
            (list(statuses),),
        ).fetchall()
        return [_load(row, TaskRecord, _TASK_TIMESTAMPS) for row in rows]

    # --- bursts ---

    def record_burst(self, burst: BurstRecord) -> None:
        data = _bind(burst, _BURST_TIMESTAMPS)
        columns = ", ".join(data)
        placeholders = ", ".join(f"%({column})s" for column in data)
        self._execute(f"INSERT INTO bursts ({columns}) VALUES ({placeholders})", data)

    def finish_burst(self, burst: BurstRecord) -> None:
        """Finalize exactly once, by compare-and-set (STORES.md).

        Rowcount decides — never a read-then-write. Zero rows means the burst was
        already finalized or killed by someone else (a second replica reconciling,
        a `stop` racing the burst's own exit), and the first writer's result
        stands: an outcome is a fact about a run that already ended, so the loser
        must not overwrite it. A4's `running ⟺ finished_at IS NULL` CHECK makes
        the same guarantee structural.
        """
        data = _bind(burst, _BURST_TIMESTAMPS)
        assignments = ", ".join(f"{column} = %({column})s" for column in BURST_FINALIZE_COLUMNS)
        cursor = self._execute(
            f"UPDATE bursts SET {assignments} WHERE burst_id = %(burst_id)s AND status = 'running'",
            data,
        )
        if cursor.rowcount == 0:
            log.warning(
                "burst already finalized — result not overwritten",
                burst=burst.burst_id,
                attempted_status=burst.status,
            )

    def get_burst(self, burst_id: str) -> BurstRecord | None:
        row = self._execute("SELECT * FROM bursts WHERE burst_id = %s", (burst_id,)).fetchone()
        return None if row is None else _load(row, BurstRecord, _BURST_TIMESTAMPS, _BURST_NUMERICS)

    def latest_archived_burst(self, task_id: str) -> BurstRecord | None:
        """The leaf of this task's session tree — the newest *dev* burst that
        archived a snapshot. Served by `bursts_session_leaf`, the partial index
        on exactly this predicate (0011 narrowed both to `role = 'dev'`): a
        tester's archived snapshot is evidence, never what the next dev burst
        resumes (B5)."""
        row = self._execute(
            "SELECT * FROM bursts WHERE task_id = %s AND session_key IS NOT NULL "
            "AND role = 'dev' ORDER BY started_at DESC, burst_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return None if row is None else _load(row, BurstRecord, _BURST_TIMESTAMPS, _BURST_NUMERICS)

    def running_bursts(self) -> list[BurstRecord]:
        rows = self._execute(
            "SELECT * FROM bursts WHERE status = 'running' ORDER BY started_at, burst_id"
        ).fetchall()
        return [_load(row, BurstRecord, _BURST_TIMESTAMPS, _BURST_NUMERICS) for row in rows]

    def bursts_started_since(self, since_iso: str) -> int:
        row = self._execute(
            "SELECT COUNT(*) AS n FROM bursts WHERE started_at >= %s", (_to_datetime(since_iso),)
        ).fetchone()
        return int(row["n"])

    # --- violations, and the outbox row each one announces ---

    def record_violation(self, violation: Violation) -> None:
        """Record the violation and announce it, in one statement (STORES.md).

        The mutation+outbox shape: a data-modifying CTE, so the row and the event
        commit together or not at all. An event without its violation would page
        someone about nothing; a violation without its event would go unnoticed,
        and "any new violations row is a page" is the platform's documented alert
        signal (TO-BE §11). Two statements could produce either.

        `pg_notify` rides along as the wake channel. It is transactional — the
        notification is delivered at commit, never before, and dropped if the
        statement rolls back — and it is not a delivery guarantee: a consumer with
        nobody listening loses it, which is why the cursor poll over `events` is
        the truth and this only ever makes it prompt.
        """
        data = _bind(violation, _VIOLATION_TIMESTAMPS)
        columns = ", ".join(data)
        placeholders = ", ".join(f"%({column})s" for column in data)
        self._execute(
            f"WITH recorded AS ("
            f"    INSERT INTO violations ({columns}) VALUES ({placeholders})"
            f"    RETURNING id, task_id, burst_id, tool, reason"
            f"), announced AS ("
            f"    INSERT INTO events (kind, payload)"
            f"    SELECT %(kind)s, jsonb_build_object("
            f"        'violation_id', id, 'task_id', task_id,"
            f"        'burst_id', burst_id, 'tool', tool, 'reason', reason)"
            f"    FROM recorded"
            f"    RETURNING id"
            f") SELECT pg_notify(%(channel)s, id::text) FROM announced",
            {**data, "kind": EVENT_VIOLATION_RECORDED, "channel": EVENT_CHANNEL},
        )

    def violations(self, task_id: str | None = None) -> list[Violation]:
        # explicit columns: the table carries an identity `id` the dataclass doesn't
        names = ", ".join(field.name for field in fields(Violation))
        if task_id is None:
            rows = self._execute(f"SELECT {names} FROM violations ORDER BY id").fetchall()
        else:
            rows = self._execute(
                f"SELECT {names} FROM violations WHERE task_id = %s ORDER BY id", (task_id,)
            ).fetchall()
        return [_load(row, Violation, _VIOLATION_TIMESTAMPS) for row in rows]

    # --- the events outbox ---

    def events(self, after_id: int = 0) -> list[Event]:
        """The outbox from a cursor forward — at-least-once, ordered, replayable.

        `after_id` is the consumer's own position; each keeps its own. There is no
        acknowledgement and no deletion here: rows age out under retention, run by
        platform_admin, once consumers have confirmed cursors past the horizon.
        """
        rows = self._execute(
            "SELECT id, kind, payload, created_at FROM events WHERE id > %s ORDER BY id",
            (after_id,),
        ).fetchall()
        return [_load(row, Event, _EVENT_TIMESTAMPS) for row in rows]

    def close(self) -> None:
        self._conn.close()
