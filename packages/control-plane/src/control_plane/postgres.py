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
from psycopg.types.json import Json

from control_plane.models import (
    BURST_FINALIZE_COLUMNS,
    EVENT_CHANNEL,
    EVENT_PRODUCT_REGISTERED,
    EVENT_PRODUCT_UPDATED,
    EVENT_VIOLATION_RECORDED,
    AuditRecord,
    BurstRecord,
    Event,
    RepositoryRecord,
    TaskRecord,
    Violation,
    utc_now,
)
from control_plane.store import RegistryError, _check_settable, _registry_transition

log = structlog.get_logger()

# Which columns are timestamps, per table. Declared, not detected: the store layer
# is the only place that knows, and a silent miss would hand a caller a datetime
# where every other backend hands it a string.
_TASK_TIMESTAMPS = ("last_burst_at", "created_at", "updated_at")
_BURST_TIMESTAMPS = ("started_at", "finished_at")
_VIOLATION_TIMESTAMPS = ("at",)
_EVENT_TIMESTAMPS = ("created_at",)
_REPOSITORY_TIMESTAMPS = ("last_burst_at", "claimed_until", "registered_at", "updated_at")
_AUDIT_TIMESTAMPS = ("at",)

# numeric → float on load; see the module docstring.
_BURST_NUMERICS = ("cost_usd",)
_REPOSITORY_NUMERICS = ("usage_share",)

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

    # --- the product registry (C1) ---
    #
    # Every mutation is one data-modifying CTE writing three tables: the change,
    # its audit row (actor, action, target, reason — TO-BE §2, "The audit log
    # starts here"), and the outbox event announcing it, with pg_notify riding
    # along as the wake channel exactly as record_violation's does. One
    # statement, so a mutation cannot exist without its trail — the same
    # guarantee the SQLite backend gives with one transaction.

    def register_repository(self, repo: RepositoryRecord, actor: str, reason: str = "") -> None:
        data = _bind(repo, _REPOSITORY_TIMESTAMPS)
        data["test_env_refs"] = Json(data["test_env_refs"])
        columns = ", ".join(data)
        placeholders = ", ".join(f"%({column})s" for column in data)
        try:
            self._execute(
                f"WITH registered AS ("
                f"    INSERT INTO repositories ({columns}) VALUES ({placeholders})"
                f"    RETURNING repository"
                f"), recorded AS ("
                f"    INSERT INTO audit (actor, action, target, reason)"
                f"    SELECT %(_actor)s, 'product.register', repository, %(_reason)s FROM registered"
                f"    RETURNING id, target"
                f"), announced AS ("
                f"    INSERT INTO events (kind, payload)"
                f"    SELECT %(_kind)s, jsonb_build_object("
                f"        'audit_id', id, 'repository', target, 'actor', %(_actor)s)"
                f"    FROM recorded RETURNING id"
                f") SELECT pg_notify(%(_channel)s, id::text) FROM announced",
                {**data, "_actor": actor, "_reason": reason,
                 "_kind": EVENT_PRODUCT_REGISTERED, "_channel": EVENT_CHANNEL},
            )
        except psycopg.errors.UniqueViolation as exc:
            raise RegistryError(f"{repo.repository} is already registered") from exc

    def get_repository(self, repository: str) -> RepositoryRecord | None:
        row = self._execute(
            "SELECT * FROM repositories WHERE repository = %s", (repository,)
        ).fetchone()
        if row is None:
            return None
        return _load(row, RepositoryRecord, _REPOSITORY_TIMESTAMPS, _REPOSITORY_NUMERICS)

    def list_repositories(self) -> list[RepositoryRecord]:
        rows = self._execute("SELECT * FROM repositories ORDER BY repository").fetchall()
        return [
            _load(row, RepositoryRecord, _REPOSITORY_TIMESTAMPS, _REPOSITORY_NUMERICS)
            for row in rows
        ]

    def set_repository_state(self, repository: str, state: str, actor: str, reason: str = "") -> None:
        action, kind = _registry_transition(state, reason)
        cursor = self._execute(
            # updated_at binds from Python, not now(): utc_now() is the one clock
            # every ledger write uses, and mixing in the server's would let a
            # sub-millisecond skew order a product's history backwards.
            "WITH changed AS ("
            "    UPDATE repositories SET state = %(state)s, updated_at = %(updated_at)s"
            "    WHERE repository = %(repository)s AND state <> %(state)s"
            "    RETURNING repository"
            "), recorded AS ("
            "    INSERT INTO audit (actor, action, target, reason)"
            "    SELECT %(actor)s, %(action)s, repository, %(reason)s FROM changed"
            "    RETURNING id, target"
            "), announced AS ("
            "    INSERT INTO events (kind, payload)"
            "    SELECT %(kind)s, jsonb_build_object("
            "        'audit_id', id, 'repository', target, 'actor', %(actor)s)"
            "    FROM recorded RETURNING id"
            ") SELECT pg_notify(%(channel)s, id::text) FROM announced",
            {"repository": repository, "state": state, "actor": actor, "action": action,
             "reason": reason, "kind": kind, "channel": EVENT_CHANNEL,
             "updated_at": _to_datetime(utc_now())},
        )
        if not cursor.fetchall():  # the CAS refused — nothing was written; say why
            existing = self.get_repository(repository)
            if existing is None:
                raise RegistryError(f"{repository} is not registered")
            raise RegistryError(f"{repository} is already {state}")

    def update_repository(self, repository: str, changes: dict, actor: str, reason: str = "") -> None:
        _check_settable(changes)
        values = dict(changes)
        if "test_env_refs" in values:
            values["test_env_refs"] = Json(values["test_env_refs"])
        # column names come from PRODUCT_SET_COLUMNS (checked above), never the caller
        assignments = ", ".join(f"{column} = %({column})s" for column in values)
        cursor = self._execute(
            f"WITH changed AS ("
            f"    UPDATE repositories SET {assignments}, updated_at = %(_updated_at)s"
            f"    WHERE repository = %(_repository)s"
            f"    RETURNING repository"
            f"), recorded AS ("
            f"    INSERT INTO audit (actor, action, target, reason)"
            f"    SELECT %(_actor)s, 'product.set', repository, %(_reason)s FROM changed"
            f"    RETURNING id, target"
            f"), announced AS ("
            f"    INSERT INTO events (kind, payload)"
            f"    SELECT %(_kind)s, jsonb_build_object("
            f"        'audit_id', id, 'repository', target, 'actor', %(_actor)s,"
            f"        'changed', %(_changed)s)"
            f"    FROM recorded RETURNING id"
            f") SELECT pg_notify(%(_channel)s, id::text) FROM announced",
            {**values, "_repository": repository, "_actor": actor, "_reason": reason,
             "_kind": EVENT_PRODUCT_UPDATED, "_changed": Json(sorted(changes)),
             "_channel": EVENT_CHANNEL, "_updated_at": _to_datetime(utc_now())},
        )
        if not cursor.fetchall():
            raise RegistryError(f"{repository} is not registered")

    def audit_trail(self, target: str | None = None) -> list[AuditRecord]:
        select = "SELECT id, actor, action, target, reason, at FROM audit"
        if target is None:
            rows = self._execute(f"{select} ORDER BY id").fetchall()
        else:
            rows = self._execute(f"{select} WHERE target = %s ORDER BY id", (target,)).fetchall()
        return [_load(row, AuditRecord, _AUDIT_TIMESTAMPS) for row in rows]

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

    def tasks_by_status(
        self, *statuses: str, repository: str | None = None
    ) -> list[TaskRecord]:
        # ORDER BY created_at alone is not a total order, and an unbroken tie is
        # resolved by the plan — index order under one, heap order under another.
        # `task_id` makes the answer the query's rather than the planner's (A7).
        # The repository scope is C4: a product's scheduler sees only its own.
        if repository is None:
            rows = self._execute(
                "SELECT * FROM tasks WHERE status = ANY(%s) ORDER BY created_at, task_id",
                (list(statuses),),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM tasks WHERE status = ANY(%s) AND repository = %s "
                "ORDER BY created_at, task_id",
                (list(statuses), repository),
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

    def running_bursts(self, repository: str | None = None) -> list[BurstRecord]:
        # a burst carries no repository column — the task it belongs to does (C4)
        if repository is None:
            rows = self._execute(
                "SELECT * FROM bursts WHERE status = 'running' ORDER BY started_at, burst_id"
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT b.* FROM bursts b JOIN tasks t ON t.task_id = b.task_id "
                "WHERE b.status = 'running' AND t.repository = %s "
                "ORDER BY b.started_at, b.burst_id",
                (repository,),
            ).fetchall()
        return [_load(row, BurstRecord, _BURST_TIMESTAMPS, _BURST_NUMERICS) for row in rows]

    def bursts_started_since(self, since_iso: str, repository: str | None = None) -> int:
        if repository is None:
            row = self._execute(
                "SELECT COUNT(*) AS n FROM bursts WHERE started_at >= %s",
                (_to_datetime(since_iso),),
            ).fetchone()
        else:
            row = self._execute(
                "SELECT COUNT(*) AS n FROM bursts b JOIN tasks t ON t.task_id = b.task_id "
                "WHERE b.started_at >= %s AND t.repository = %s",
                (_to_datetime(since_iso), repository),
            ).fetchone()
        return int(row["n"])

    def touch_repository_burst(self, repository: str, at: str) -> None:
        """Stamp the least-recently-served cursor (C4) — the fleet marks the
        product whose turn it just took. Operational state, not an operator
        mutation: no audit row, no event, `updated_at` untouched."""
        cursor = self._execute(
            "UPDATE repositories SET last_burst_at = %s WHERE repository = %s",
            (_to_datetime(at), repository),
        )
        if cursor.rowcount == 0:
            log.warning("touch of an unregistered product ignored", repo=repository)

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
