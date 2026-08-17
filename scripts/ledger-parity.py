"""Ledger parity: one scenario, two backends, one comparable dump.

The instrument behind the byte-for-byte gates (A7/#18, and C4/#26 after it). Run
the same scenario twice — once with `LEDGER_DSN` + `ARCHIVE_URL` set, once with
both unset — then point this at the two ledgers:

    uv run python scripts/ledger-parity.py <data-dir>/ledger.sqlite3 "$LEDGER_DSN"

"The same" cannot mean equal bytes: ids are random, timestamps advance, and a
model's cost is its own business. So every field lands in one of three buckets.

  strict     compared after interning, and a difference fails the run. Everything
             about shape, status, and lineage.
  excused    not compared as values, because they cannot repeat — but interned or
             shape-checked instead. A burst id becomes B1, B2 … in start order,
             and `resumed_from_burst_id` becomes *its target's* symbol, so "resumed
             from the burst before it" turns into a comparable value rather than an
             opaque hex string. Timestamps must parse and carry an offset.
  variance   the agent's numbers (cost, tokens). Printed, never compared: a
             difference there is the model's, not the store's.

`num_turns`, `exit_code` and `status` are deliberately strict. A model that took a
different number of turns is worth seeing as a failure and then reading, rather
than filing under variance in advance.

A caveat, since a clean report is easy to over-read: this compares the rows two
runs left behind. Walls that reject a write — the seeded-role trigger, the grants,
one-running-burst-per-task — leave no rows and live in tests/test_schema_walls.py.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

# The tables both backends carry. The org tables (repositories, roles,
# subscriptions, memory_entries, audit) are Postgres-only by design — the
# single-node ledger is the MVP's four tables, and STORES.md says why.
SHARED_TABLES = ("tasks", "bursts", "violations", "events")

# Canonical row order — never the storage order, which neither backend promises.
# Bursts sort by start, so the interned symbols read B1, B2 … in the order the run
# produced them.
ORDER = {
    "tasks": ("created_at", "task_id"),
    "bursts": ("started_at", "burst_id"),
    "violations": ("at", "id"),
    "events": ("id",),
}

# (table, column) → symbol prefix. The same value always gets the same symbol, so a
# reference and its referent collapse into one comparable token.
INTERNED = {
    ("bursts", "burst_id"): "B",
    ("bursts", "resumed_from_burst_id"): "B",
    ("bursts", "trace_id"): "T",
    ("bursts", "session_id"): "S",
    ("bursts", "session_checksum"): "C",
    ("tasks", "session_id"): "S",
    ("violations", "burst_id"): "B",
}

TIMESTAMPS = {
    ("tasks", "last_burst_at"),
    ("tasks", "created_at"),
    ("tasks", "updated_at"),
    ("bursts", "started_at"),
    ("bursts", "finished_at"),
    ("violations", "at"),
    ("events", "created_at"),
}

VARIANCE = {("bursts", "cost_usd"), ("bursts", "input_tokens"), ("bursts", "output_tokens")}

problems: list[str] = []
variance: list[str] = []


class Symbols:
    """Order-assigned names for values that cannot repeat between runs."""

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}

    def intern(self, prefix: str, value: str) -> str:
        key = (prefix, value)
        if key not in self._seen:
            self._counts[prefix] = self._counts.get(prefix, 0) + 1
            self._seen[key] = f"{prefix}{self._counts[prefix]}"
        return self._seen[key]


def _comparable(value: object) -> object:
    """Postgres hands back datetimes and Decimals where SQLite hands back text."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _canonical(table: str, rows: list[dict], symbols: Symbols, side: str) -> list[dict]:
    ordered = sorted(
        rows,
        key=lambda row: tuple(
            "" if row[column] is None else str(_comparable(row[column])) for column in ORDER[table]
        ),
    )
    out: list[dict] = []
    for row in ordered:
        canonical: dict[str, object] = {}
        for column, raw in row.items():
            value = _comparable(raw)
            spot = (table, column)

            if spot in TIMESTAMPS:
                if value is not None:
                    try:
                        stamp = datetime.fromisoformat(str(value))
                    except ValueError:
                        problems.append(f"{side} {table}.{column}: not ISO-8601: {value!r}")
                    else:
                        if stamp.tzinfo is None:
                            problems.append(f"{side} {table}.{column}: no offset: {value!r}")
                canonical[column] = None if value is None else "<ts>"
            elif spot in VARIANCE:
                canonical[column] = None if value is None else f"<{type(value).__name__}>"
                variance.append(f"{side:8} {table}.{column} = {value!r}")
            elif spot in INTERNED and value is not None:
                if column == "session_checksum" and not _is_sha256(str(value)):
                    problems.append(f"{side} bursts.session_checksum is not a sha256: {value!r}")
                canonical[column] = symbols.intern(INTERNED[spot], str(value))
            elif column == "session_key" and value is not None:
                # sessions/{task_id}/{burst_id}/session.tar.gz — the burst id becomes
                # its symbol, so the whole key scheme is compared, not its presence
                head, _, tail = str(value).rpartition("/")
                prefix, _, burst = head.rpartition("/")
                canonical[column] = f"{prefix}/{symbols.intern('B', burst)}/{tail}"
            elif column == "payload" and value is not None:
                loaded = json.loads(value) if isinstance(value, str) else value
                if loaded.get("burst_id"):
                    loaded["burst_id"] = symbols.intern("B", loaded["burst_id"])
                # sort_keys, because jsonb does not keep the order it was given
                canonical[column] = json.dumps(loaded, sort_keys=True)
            else:
                canonical[column] = value
        out.append(canonical)
    return out


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def dump_sqlite(path: Path) -> tuple[dict[str, list[dict]], dict[str, list[str]]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    symbols, tables, columns = Symbols(), {}, {}
    for table in _mint_order():
        columns[table] = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
        tables[table] = _canonical(table, rows, symbols, "sqlite")
    connection.close()
    return tables, columns


def dump_postgres(dsn: str) -> tuple[dict[str, list[dict]], dict[str, list[str]]]:
    connection = psycopg.connect(dsn, row_factory=dict_row)
    symbols, tables, columns = Symbols(), {}, {}
    for table in _mint_order():
        columns[table] = [
            row["column_name"]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
                (table,),
            ).fetchall()
        ]
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        tables[table] = _canonical(table, rows, symbols, "postgres")
    connection.close()
    return tables, columns


def _mint_order() -> tuple[str, ...]:
    """Bursts first: it is where B/T/S/C symbols are minted, and every other table
    only references them."""
    return ("bursts", *[table for table in SHARED_TABLES if table != "bursts"])


def compare(
    left: dict[str, list[dict]],
    right: dict[str, list[dict]],
    left_columns: dict[str, list[str]],
    right_columns: dict[str, list[str]],
) -> int:
    differences = 0
    for table in SHARED_TABLES:
        lcolumns, rcolumns = set(left_columns[table]), set(right_columns[table])
        if lcolumns != rcolumns:
            differences += 1
            print(f"  {table}: column sets differ")
            print(f"    sqlite only:   {sorted(lcolumns - rcolumns)}")
            print(f"    postgres only: {sorted(rcolumns - lcolumns)}")
        lrows, rrows = left[table], right[table]
        if len(lrows) != len(rrows):
            differences += 1
            print(f"  {table}: {len(lrows)} rows on sqlite, {len(rrows)} on postgres")
            continue
        shared = sorted(lcolumns & rcolumns)
        found = 0
        for index, (lrow, rrow) in enumerate(zip(lrows, rrows, strict=True)):
            for column in shared:
                if lrow[column] != rrow[column]:
                    found += 1
                    print(f"  {table}[{index}].{column}: {lrow[column]!r} vs {rrow[column]!r}")
        differences += found
        if not found:
            print(f"  {table}: {len(lrows)} rows × {len(shared)} columns — identical")
    return differences


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    left, left_columns = dump_sqlite(Path(argv[1]))
    right, right_columns = dump_postgres(argv[2])

    print("== field for field ==")
    differences = compare(left, right, left_columns, right_columns)
    print("\n== run variance (the agent's numbers, not the store's) ==")
    for line in variance:
        print(f"  {line}")
    if problems:
        print("\n== shape problems ==")
        for problem in problems:
            print(f"  {problem}")
    print(f"\n{differences} strict differences, {len(problems)} shape problems")
    return 1 if differences or problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
