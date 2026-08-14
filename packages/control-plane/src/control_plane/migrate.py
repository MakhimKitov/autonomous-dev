"""Schema migrations — numbered SQL files, applied once, in order.

The MVP's ``_ensure_column`` guesswork doesn't survive a service database: it can
add a column but never a constraint, an index, or a backfill, and it leaves no
record of what a given database has seen. Stage A replaces it with the ordinary
thing — ``migrations/NNNN_slug.sql`` applied by this module against
``schema_migrations(version, applied_at)`` (STORES.md "Schema — canonical DDL").

The rules, all enforced below rather than documented and hoped for:

- **Ordered and contiguous.** Versions run ``0001``, ``0002``, … with no gaps and
  no duplicates; two branches that both add ``0007`` collide at discovery, in CI,
  instead of applying in whichever order the filesystem felt like.
- **Applied once.** A version present in the registry is skipped, so ``migrate``
  is idempotent — the second run is a no-op that writes nothing.
- **Never backwards.** A registry naming versions this build doesn't ship means a
  stale checkout pointed at a newer database; that's an error, not a fresh start.
- **One transaction per file.** Postgres DDL is transactional, so a failing
  migration leaves nothing half-applied, and the versions before it stay applied
  and recorded — the state after a failure is a prefix, always.
- **One migrator at a time**, under an advisory lock. This is the one place a
  lock may outlive a statement (STORES.md's discipline governs *runtime* writes;
  migrations are admin-time, run by ``platform_admin`` before replicas start).
  The lock is session-scoped, so a killed migrator releases it by disconnecting —
  never a stuck lock needing an operator.

The SQLite ledger is not migrated here: it is the single-node variant, it
self-bootstraps from the same canonical DDL by hand (``store.py``), and it goes
away as an origin at A6's backfill. ``LEDGER_DSN`` unset means there is nothing
to migrate.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from pathlib import Path

import psycopg
import structlog

log = structlog.get_logger()

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
REGISTRY = "schema_migrations"
FILENAME = re.compile(r"^(\d{4})_([a-z0-9-]+)\.sql$")

# pg_advisory_lock key: derived from the registry's name so it is stable across
# builds without a magic number to keep in sync.
MIGRATION_LOCK_KEY = zlib.crc32(b"control_plane.schema_migrations")


class MigrationError(RuntimeError):
    """The migration set itself is wrong — bad name, gap, duplicate, or drift."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: str  # '0001' — zero-padded, the registry's key
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def __str__(self) -> str:
        return f"{self.version}_{self.name}"


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Every migration this build ships, in apply order. Raises on a bad set."""
    found: dict[str, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        matched = FILENAME.match(path.name)
        if matched is None:
            raise MigrationError(f"migration filename must be NNNN_slug.sql: {path.name}")
        version, name = matched.group(1), matched.group(2)
        if version in found:
            raise MigrationError(f"duplicate migration {version}: {found[version]}, {path.stem}")
        found[version] = Migration(version=version, name=name, path=path)

    ordered = [found[version] for version in sorted(found)]
    for expected, migration in enumerate(ordered, start=1):
        if int(migration.version) != expected:
            raise MigrationError(
                f"migration versions must be contiguous from 0001 — "
                f"expected {expected:04d}, found {migration.version}"
            )
    return ordered


def pending(applied: set[str], available: list[Migration]) -> list[Migration]:
    """What's left to apply. Raises if the database is ahead of this build."""
    vanished = applied - {migration.version for migration in available}
    if vanished:
        raise MigrationError(
            f"the database has migrations this build does not ship: {', '.join(sorted(vanished))} "
            f"— stale checkout against a newer ledger?"
        )
    return [migration for migration in available if migration.version not in applied]


def _applied_versions(conn: psycopg.Connection) -> set[str]:
    """Recorded versions — empty if the registry itself isn't there yet."""
    if conn.execute("SELECT to_regclass(%s)", (REGISTRY,)).fetchone()[0] is None:
        return set()
    rows = conn.execute(f"SELECT version FROM {REGISTRY}").fetchall()
    return {row[0] for row in rows}


def status(dsn: str) -> tuple[list[str], list[Migration]]:
    """(applied versions, pending migrations). Reads only — writes nothing."""
    available = discover()
    with psycopg.connect(dsn) as conn:
        applied = _applied_versions(conn)
    return sorted(applied), pending(applied, available)


def migrate(dsn: str) -> list[Migration]:
    """Apply every pending migration. Returns what this call applied."""
    available = discover()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {REGISTRY} ("
            "    version    text PRIMARY KEY,"
            "    applied_at timestamptz NOT NULL DEFAULT now())"
        )
        todo = pending(_applied_versions(conn), available)
        for migration in todo:
            with conn.transaction():  # DDL and its registry row commit together
                conn.execute(migration.sql)
                conn.execute(f"INSERT INTO {REGISTRY} (version) VALUES (%s)", (migration.version,))
            log.info("migration applied", version=migration.version, name=migration.name)
        return todo
