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
data-modifying CTE rather than two statements in a transaction. Every task write
the scheduler makes is a compare-and-set (`transition_task`, `finish_burst`) or a
lease statement (`claim_next`, `renew_lease` — E1). There is no method here that
could keep a transaction open across application logic or I/O (STORES.md,
"transactional, but not interactive"), no ORM to accidentally introduce one, and
`in_transaction` is how the suite checks that after every call.

The one thing wider than a statement is admission's advisory lock (`claim_next`):
a session-level `pg_try_advisory_lock` around two statements — the rail's
aggregate and the claim — held for microseconds inside this method and released
before it returns; a replica that dies holding it releases it with its session.
It is what makes the daily rail exact across replicas, and it fences no I/O.

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
    EVENT_ROLE_UPDATED,
    EVENT_VIOLATION_RECORDED,
    AuditRecord,
    BurstRecord,
    Event,
    RepositoryRecord,
    RoleRecord,
    TaskRecord,
    Violation,
    utc_now,
)
from control_plane.roles import RoleError
from control_plane.roles import check_settable as check_role_settable
from control_plane.store import RegistryError, _check_settable, _check_transition, _registry_transition

log = structlog.get_logger()

# Which columns are timestamps, per table. Declared, not detected: the store layer
# is the only place that knows, and a silent miss would hand a caller a datetime
# where every other backend hands it a string.
_TASK_TIMESTAMPS = ("last_burst_at", "claimed_until", "created_at", "updated_at")

# The admission fence (TO-BE §5): one key, cluster-wide. Admission is the single
# serialized section the design allows, and it serializes an aggregate decision
# (today per product, later fair share across them), so one key is the honest
# scope. Any int64 would do; this one spells what it is.
ADMISSION_LOCK_KEY = 0x41444D49  # 'ADMI'

_BURST_TIMESTAMPS = ("started_at", "finished_at")
_VIOLATION_TIMESTAMPS = ("at",)
_EVENT_TIMESTAMPS = ("created_at",)
_REPOSITORY_TIMESTAMPS = ("last_burst_at", "claimed_until", "registered_at", "updated_at")
_AUDIT_TIMESTAMPS = ("at",)
_ROLE_TIMESTAMPS = ("created_at", "updated_at")

# numeric → float on load; see the module docstring.
_BURST_NUMERICS = ("cost_usd",)
_REPOSITORY_NUMERICS = ("usage_share",)
_ROLE_NUMERICS = ("max_budget_usd",)

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

    # --- roles (D5) ---
    #
    # `role set` is the registry CTE shape again — the change, its audit row,
    # its event, pg_notify — under the UPDATE grant 0014 gives platform_app.
    # The open fields are checked by name before any SQL; a wall field that got
    # past that meets roles_pipeline_guard (0002) as a RestrictViolation.

    def list_roles(self) -> list[RoleRecord]:
        rows = self._execute("SELECT * FROM roles ORDER BY kind DESC, role").fetchall()
        return [_load(row, RoleRecord, _ROLE_TIMESTAMPS, _ROLE_NUMERICS) for row in rows]

    def get_role(self, role: str) -> RoleRecord | None:
        row = self._execute("SELECT * FROM roles WHERE role = %s", (role,)).fetchone()
        return None if row is None else _load(row, RoleRecord, _ROLE_TIMESTAMPS, _ROLE_NUMERICS)

    def update_role(self, role: str, changes: dict, actor: str, reason: str = "") -> None:
        check_role_settable(changes)
        # column names come from ROLE_SET_COLUMNS (checked above), never the caller
        assignments = ", ".join(f"{column} = %({column})s" for column in changes)
        try:
            cursor = self._execute(
                f"WITH changed AS ("
                f"    UPDATE roles SET {assignments}, updated_at = %(_updated_at)s"
                f"    WHERE role = %(_role)s"
                f"    RETURNING role"
                f"), recorded AS ("
                f"    INSERT INTO audit (actor, action, target, reason)"
                f"    SELECT %(_actor)s, 'role.set', role, %(_reason)s FROM changed"
                f"    RETURNING id, target"
                f"), announced AS ("
                f"    INSERT INTO events (kind, payload)"
                f"    SELECT %(_kind)s, jsonb_build_object("
                f"        'audit_id', id, 'role', target, 'actor', %(_actor)s,"
                f"        'changed', %(_changed)s)"
                f"    FROM recorded RETURNING id"
                f") SELECT pg_notify(%(_channel)s, id::text) FROM announced",
                {**changes, "_role": role, "_actor": actor, "_reason": reason,
                 "_kind": EVENT_ROLE_UPDATED, "_changed": Json(sorted(changes)),
                 "_channel": EVENT_CHANNEL, "_updated_at": _to_datetime(utc_now())},
            )
        except psycopg.errors.RestrictViolation as exc:  # the trigger — a wall field got through
            raise RoleError(f"{role}: {exc.diag.message_primary}") from exc
        if not cursor.fetchall():
            raise RoleError(f"no role named {role!r}")

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

    # --- transitions and leases (E1) ---
    #
    # The scheduler's task writes, each one statement: insert-if-absent,
    # compare-and-set on (status, holder) with RETURNING, the lease claim with
    # FOR UPDATE SKIP LOCKED, the heartbeat renewal. Rowcount decides everywhere;
    # nothing here reads and then writes. Time enters a comparison only as the
    # database's now() (STORES.md, "One clock"): lease expiry and the failure
    # cooldown are the ledger's arithmetic, so replica clock skew cannot decide
    # who owns what.

    def create_task(self, task: TaskRecord) -> bool:
        """Insert the row if the ledger has never seen the task; False when it
        has — untouched, whatever state it is in. Two replicas claiming one
        issue compute the same id (A6), and the second learns it lost here."""
        data = _bind(task, _TASK_TIMESTAMPS)
        columns = ", ".join(data)
        placeholders = ", ".join(f"%({column})s" for column in data)
        cursor = self._execute(
            f"INSERT INTO tasks ({columns}) VALUES ({placeholders}) ON CONFLICT DO NOTHING", data
        )
        return cursor.rowcount == 1

    def transition_task(
        self, task_id: str, changes: dict, *, expected_status: str, expected_holder: str | None
    ) -> TaskRecord | None:
        """Compare-and-set (STORES.md, "Transition"): write `changes` if the row
        still reads (`expected_status`, `expected_holder`); the row as written,
        or None when it had moved — the caller lost the race and writes nothing.
        `IS NOT DISTINCT FROM` is the null-safe equality: an unleased row's
        holder is NULL."""
        _check_transition(changes)
        values = dict(changes)
        for column in _TASK_TIMESTAMPS:
            if column in values:
                values[column] = _to_datetime(values[column])
        # column names come from TASK_TRANSITION_COLUMNS (checked above), never the caller
        assignments = ", ".join(f"{column} = %({column})s" for column in values)
        row = self._execute(
            f"UPDATE tasks SET {assignments}, updated_at = %(_updated_at)s "
            f"WHERE task_id = %(_task_id)s AND status = %(_status)s "
            f"  AND claimed_by IS NOT DISTINCT FROM %(_holder)s "
            f"RETURNING *",
            {**values, "_updated_at": _to_datetime(utc_now()), "_task_id": task_id,
             "_status": expected_status, "_holder": expected_holder},
        ).fetchone()
        return None if row is None else _load(row, TaskRecord, _TASK_TIMESTAMPS)

    def claim_next(
        self, repository: str, replica: str, *, ceiling: int, since: str, lease_s: float, cooldown_s: float
    ) -> TaskRecord | None:
        """Admission (TO-BE §5) under the one advisory lock: the daily rail's
        aggregate, then the claim — the oldest `claimed` task of the product
        whose lease nobody holds and whose failure cooldown has passed, leased to
        `replica` for `lease_s` by the statement that picks it (STORES.md,
        "Claim — a lease is data"). `FOR UPDATE SKIP LOCKED` makes two replicas
        inside the same microsecond take different rows rather than wait.

        The rail counts *admitted* work, not recorded bursts: bursts started
        since `since`, plus spawns in flight — a live lease with no running burst
        row yet. Under the lock that sum is exact across replicas; without the
        second term two of them would each see "one under the ceiling" and both
        spawn. Its one imprecision is conservative (a burst that has just
        finished but whose task has not yet moved counts twice for a few seconds
        — a spawn deferred a tick, never one over the rail).

        The lock is session-level and try-only: a peer holding it means a peer is
        admitting right now, and this replica idles the tick rather than queue
        behind it — admission is microseconds, the next tick is a minute away. A
        replica that dies inside releases it with its session; `finally` releases
        it on every other path.
        """
        fenced = self._execute(
            "SELECT pg_try_advisory_lock(%s) AS held", (ADMISSION_LOCK_KEY,)
        ).fetchone()["held"]
        if not fenced:
            log.info("admission fence held by a peer — idling this tick", repo=repository)
            return None
        try:
            admitted = self._execute(
                "SELECT (SELECT count(*) FROM bursts b JOIN tasks t ON t.task_id = b.task_id"
                "        WHERE t.repository = %(repository)s AND b.started_at >= %(since)s)"
                "     + (SELECT count(*) FROM tasks t"
                "        WHERE t.repository = %(repository)s AND t.claimed_until > now()"
                "          AND NOT EXISTS (SELECT 1 FROM bursts b"
                "                          WHERE b.task_id = t.task_id AND b.status = 'running')) AS n",
                {"repository": repository, "since": _to_datetime(since)},
            ).fetchone()["n"]
            if admitted >= ceiling:
                log.warning("daily burst ceiling reached — not spawning", repo=repository, ceiling=ceiling)
                return None
            row = self._execute(
                "UPDATE tasks SET claimed_by = %(replica)s,"
                "                 claimed_until = now() + make_interval(secs => %(lease_s)s) "
                "WHERE task_id = ("
                "    SELECT task_id FROM tasks"
                "    WHERE repository = %(repository)s AND status = 'claimed'"
                "      AND (claimed_until IS NULL OR claimed_until < now())"
                "      AND (attempts = 0 OR last_burst_at IS NULL"
                "           OR last_burst_at + make_interval(secs => %(cooldown_s)s) <= now())"
                "    ORDER BY created_at, task_id"
                "    FOR UPDATE SKIP LOCKED"
                "    LIMIT 1"
                ") RETURNING *",
                {"replica": replica, "lease_s": float(lease_s), "repository": repository,
                 "cooldown_s": float(cooldown_s)},
            ).fetchone()
        finally:
            self._execute("SELECT pg_advisory_unlock(%s)", (ADMISSION_LOCK_KEY,))
        return None if row is None else _load(row, TaskRecord, _TASK_TIMESTAMPS)

    def renew_lease(self, task_id: str, replica: str, lease_s: float) -> bool:
        """The heartbeat: push the lease out by `lease_s`, only while `replica`
        still holds it. False means a peer has taken the task — the caller's
        work is no longer wanted and it fences itself off."""
        cursor = self._execute(
            "UPDATE tasks SET claimed_until = now() + make_interval(secs => %s) "
            "WHERE task_id = %s AND claimed_by = %s",
            (float(lease_s), task_id, replica),
        )
        return cursor.rowcount == 1

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._execute("SELECT * FROM tasks WHERE task_id = %s", (task_id,)).fetchone()
        return None if row is None else _load(row, TaskRecord, _TASK_TIMESTAMPS)

    def tasks_by_status(
        self, *statuses: str, repository: str | None = None, orphaned: bool = False
    ) -> list[TaskRecord]:
        # ORDER BY created_at alone is not a total order, and an unbroken tie is
        # resolved by the plan — index order under one, heap order under another.
        # `task_id` makes the answer the query's rather than the planner's (A7).
        # The repository scope is C4: a product's scheduler sees only its own.
        # `orphaned` (E1) keeps only rows no live lease covers — what a dead
        # replica leaves behind, and what the reconciler may touch.
        query = "SELECT * FROM tasks WHERE status = ANY(%s)"
        params: list[Any] = [list(statuses)]
        if repository is not None:
            query += " AND repository = %s"
            params.append(repository)
        if orphaned:
            query += " AND (claimed_until IS NULL OR claimed_until < now())"
        rows = self._execute(query + " ORDER BY created_at, task_id", params).fetchall()
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

    def running_bursts(
        self, repository: str | None = None, *, orphaned: bool = False
    ) -> list[BurstRecord]:
        # a burst carries no repository column and no lease — the task it belongs
        # to does (C4, E1), so both scopes join it. `orphaned`: running bursts of
        # tasks no live lease covers — a live lease is a peer's proof of life,
        # from any host, and the reconciler leaves those alone.
        query = "SELECT b.* FROM bursts b LEFT JOIN tasks t ON t.task_id = b.task_id WHERE b.status = 'running'"
        params: list[Any] = []
        if repository is not None:
            query += " AND t.repository = %s"
            params.append(repository)
        if orphaned:
            query += " AND (t.claimed_until IS NULL OR t.claimed_until < now())"
        rows = self._execute(query + " ORDER BY b.started_at, b.burst_id", params).fetchall()
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

    @property
    def in_transaction(self) -> bool:
        """Whether a statement left a transaction open — the E1 assertion seam.
        Always False between calls, by construction: the connection is
        autocommit and no method opens a block."""
        return self._conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    def close(self) -> None:
        self._conn.close()
