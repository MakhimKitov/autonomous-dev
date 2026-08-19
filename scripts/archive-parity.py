"""Archive parity: the same snapshots, in a bucket and in a directory.

`ledger-parity.py`'s twin, for the other store. Run the same scenario against both
backends, then:

    uv run python scripts/archive-parity.py <single-node-data-dir> "$LEDGER_DSN" "$ARCHIVE_URL"

Everything goes through the `SessionArchive` protocol, so what is compared is what
the platform sees, not what the storage happens to hold — and `get()` verifying the
checksum the ledger recorded *is* one of the checks, not a step before it.

Members split three ways, for the same reason the ledger dump splits fields:

  ours        burst-result.json, violations.jsonl — the platform wrote them, so they
              must match, and the violations feed is compared entry by entry.
  transcript  projects/<slug>/<session-uuid>.jsonl — the count is comparable, the
              name is the session's own. It reappearing in every later snapshot is
              what resume looks like from the archive's side.
  the CLI's   .claude.json, backups/, .last-cleanup … — the agent CLI's own
              housekeeping, written on its own schedule inside the container.
              Reported, never diffed: nothing about the archive decides whether a
              cleanup ran mid-burst.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import psycopg

from control_plane.objectstore import ObjectSessionArchive
from control_plane.store import EXCLUDE_FROM_ARCHIVE, LocalSessionArchive

OURS = {"burst-result.json", "violations.jsonl"}
TRANSCRIPT = re.compile(r"^projects/[^/]+/[0-9a-f-]{36}\.jsonl$")
BURSTS = "SELECT session_key, session_checksum FROM bursts ORDER BY started_at"

problems: list[str] = []


def members(archive, key: str, checksum: str, into: Path) -> set[str]:
    """The snapshot's contents, fetched the way the platform fetches them.

    `get` raises unless the bytes hash to the checksum the ledger recorded, so this
    is also the bytes-match-the-ledger check.
    """
    archive.get(key, into, checksum)
    return {str(path.relative_to(into)) for path in into.rglob("*") if path.is_file()}


def classify(names: set[str]) -> tuple[set[str], set[str], set[str]]:
    ours = {name for name in names if name in OURS}
    transcripts = {name for name in names if TRANSCRIPT.match(name)}
    return ours, transcripts, names - ours - transcripts


def read_feed(root: Path) -> list[dict]:
    feed = root / "violations.jsonl"
    if not feed.is_file():
        return []
    return [json.loads(line) for line in feed.read_text().splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    data_dir, dsn, url = Path(argv[1]), argv[2], argv[3]

    local = LocalSessionArchive(data_dir)
    remote = ObjectSessionArchive.from_url(url)
    single = sqlite3.connect(data_dir / "ledger.sqlite3").execute(BURSTS).fetchall()
    service = psycopg.connect(dsn).execute(BURSTS).fetchall()

    print(f"== key shape: {len(single)} snapshots local, {len(service)} remote ==")
    if len(single) != len(service):
        problems.append(f"{len(single)} snapshots local, {len(service)} remote")
    shape = ""

    def prefix(key: str) -> str:
        """Everything above `<burst>/session.tar.gz` — the part both sides must share."""
        return key.rpartition("/")[0].rpartition("/")[0]

    for (lkey, _), (rkey, _) in zip(single, service, strict=False):
        if prefix(lkey) != prefix(rkey):
            problems.append(f"key prefixes differ: {prefix(lkey)} vs {prefix(rkey)}")
        shape = prefix(lkey)
    print(f"   both keyed {shape}/<burst>/session.tar.gz")

    print("\n== the ledger is the checksum authority ==")
    for side, rows, archive in (("local ", single, local), ("remote", service, remote)):
        for key, checksum in rows:
            recorded = archive.recorded_checksum(key)
            if recorded != checksum:
                problems.append(f"{side} {key}: archive says {recorded}, ledger says {checksum}")
        print(f"   {side}: {len(rows)} snapshots, every recorded checksum equals the ledger's")

    print("\n== what the snapshots hold (get() also proves bytes ↔ checksum) ==")
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        for index, ((lkey, lsum), (rkey, rsum)) in enumerate(zip(single, service, strict=False), 1):
            lroot, rroot = root / f"local-{index}", root / f"remote-{index}"
            lours, ltranscripts, lcli = classify(members(local, lkey, lsum, lroot))
            rours, rtranscripts, rcli = classify(members(remote, rkey, rsum, rroot))

            sides = (
                ("local", lours | ltranscripts | lcli),
                ("remote", rours | rtranscripts | rcli),
            )
            for side, names in sides:
                leaked = {name for name in names if Path(name).name in EXCLUDE_FROM_ARCHIVE}
                if leaked:
                    problems.append(f"snapshot {index} {side}: credential files inside: {leaked}")
            if lours != rours:
                problems.append(f"snapshot {index}: our own files differ: {lours ^ rours}")
            if len(ltranscripts) != len(rtranscripts):
                problems.append(
                    f"snapshot {index}: {len(ltranscripts)} transcripts local,"
                    f" {len(rtranscripts)} remote"
                )
            print(
                f"   snapshot {index}: ours {sorted(lours) or '—'}"
                f" · transcripts {len(ltranscripts)}/{len(rtranscripts)}"
                f" · CLI files {len(lcli)}/{len(rcli)}"
            )

            lfeed, rfeed = read_feed(lroot), read_feed(rroot)
            if lfeed or rfeed:
                # `at` is when the wall fired — the one field two runs cannot share
                stripped = [
                    [{k: v for k, v in entry.items() if k != "at"} for entry in feed]
                    for feed in (lfeed, rfeed)
                ]
                same = stripped[0] == stripped[1]
                print(
                    f"     violations.jsonl: {len(lfeed)} vs {len(rfeed)} entries,"
                    f" minus `at` — {'identical' if same else 'DIFFER'}"
                )
                if not same:
                    problems.append(f"snapshot {index}: the violations feed differs")

        print("\n== the last snapshot, member by member (local) ==")
        last = root / f"local-{len(single)}"
        for name in sorted(str(p.relative_to(last)) for p in last.rglob("*") if p.is_file()):
            tag = "ours" if name in OURS else ("transcript" if TRANSCRIPT.match(name) else "cli")
            print(f"   {tag:11} {name}")

    print(f"\n{len(problems)} problems")
    for problem in problems:
        print(f"  !! {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
