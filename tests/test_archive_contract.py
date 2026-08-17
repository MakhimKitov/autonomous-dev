"""The `SessionArchive` contract, run against every backend there is.

One suite, parameterized over `LocalSessionArchive` and `ObjectSessionArchive`,
for the same reason the ledger has one (`test_ledger_contract.py`): the two are
one design with and without a network, so a test that passes on one and fails on
the other is a bug by definition. The object backend is skipped unless
`ARCHIVE_URL` points at a bucket; when it is set, each test gets its own key
prefix, so a run never sees another run's objects.

Three properties carry the whole design, and each is provoked rather than
asserted: snapshots are write-once, bytes are checked against the checksum the
*ledger* recorded, and credentials never enter the archive at all.
"""

import hashlib
import os
from uuid import uuid4

import pytest
from control_plane.store import (
    ARCHIVE_URL_ENV,
    ArchiveError,
    LocalSessionArchive,
    session_key,
)

URL = os.environ.get(ARCHIVE_URL_ENV, "").strip()
BACKENDS = ["local"] + (["s3"] if URL else [])


@pytest.fixture
def s3_archive():
    """The object backend alone, for what only it can do. Skips without a bucket."""
    if not URL:
        pytest.skip(f"{ARCHIVE_URL_ENV} unset — no bucket")
    from control_plane.objectstore import ObjectSessionArchive

    # a prefix per test: the bucket outlives the run, the test's objects do not
    backend = ObjectSessionArchive.from_url(URL)
    backend.prefix = f"{backend.prefix}/test-{uuid4().hex[:8]}".strip("/")
    return backend


@pytest.fixture(params=BACKENDS)
def archive(request, tmp_path):
    """An empty archive of each backend in turn — the contract every one holds."""
    if request.param == "local":
        return LocalSessionArchive(tmp_path / "archive")
    return request.getfixturevalue("s3_archive")


@pytest.fixture
def a_session(tmp_path):
    """A session volume with something in it, shaped like the real thing."""
    volume = tmp_path / "volume"
    (volume / "projects" / "p").mkdir(parents=True)
    (volume / "projects" / "p" / "sess.jsonl").write_text('{"turn":1}\n', encoding="utf-8")
    (volume / "settings.json").write_text("{}", encoding="utf-8")
    return volume


KEY = session_key("42", "b1")


def test_a_snapshot_comes_back_byte_for_byte(archive, a_session, tmp_path):
    checksum = archive.put(KEY, a_session)
    assert len(checksum) == 64

    restored = tmp_path / "restored"
    archive.get(KEY, restored, checksum)
    assert (restored / "projects" / "p" / "sess.jsonl").read_text() == '{"turn":1}\n'
    assert (restored / "settings.json").read_text() == "{}"


def test_snapshots_are_write_once(archive, a_session):
    """The archive is immutable storage. A key that exists is never rewritten —
    a retry is a new burst, so a new key, and branching is a ledger write."""
    archive.put(KEY, a_session)

    with pytest.raises(ArchiveError, match="write-once"):
        archive.put(KEY, a_session)


def test_a_missing_snapshot_is_an_error_not_an_empty_volume(archive, tmp_path):
    """`get` is only ever called for a key the ledger handed over, so a key with
    nothing behind it means the two stores disagree — which is worth stopping for,
    not starting a burst on an empty volume for."""
    with pytest.raises(ArchiveError, match="no snapshot"):
        archive.get(session_key("42", "never-archived"), tmp_path / "dest", "0" * 64)


def test_bytes_are_checked_against_the_ledgers_checksum(archive, a_session, tmp_path):
    """The ledger is the checksum authority: `get` takes the recorded value and
    refuses to extract anything that doesn't match it."""
    archive.put(KEY, a_session)

    with pytest.raises(ArchiveError, match="checksum mismatch"):
        archive.get(KEY, tmp_path / "dest", "b" * 64)
    assert not (tmp_path / "dest").exists()  # nothing reached the volume


def test_the_archive_records_the_checksum_too(archive, a_session):
    """Alongside the object, never instead of the ledger. This is how the number
    gets home when the control plane never held the bytes (presigned PUT, G2)."""
    checksum = archive.put(KEY, a_session)
    assert archive.recorded_checksum(KEY) == checksum
    assert archive.recorded_checksum(session_key("42", "nothing-here")) is None


def test_credentials_never_enter_the_archive(archive, tmp_path):
    """CLAUDE_CONFIG_DIR *is* the harvested volume, and the CLI may write
    .credentials.json into it. The exclusion is unconditional, on every backend."""
    volume = tmp_path / "volume"
    volume.mkdir()
    (volume / ".credentials.json").write_text('{"token":"secret"}', encoding="utf-8")
    (volume / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    (volume / "keep.txt").write_text("keep", encoding="utf-8")

    checksum = archive.put(KEY, volume)
    restored = tmp_path / "restored"
    archive.get(KEY, restored, checksum)

    assert (restored / "keep.txt").is_file()
    assert not (restored / ".credentials.json").exists()
    assert not (restored / "auth.json").exists()


def test_snapshots_of_one_task_do_not_collide(archive, a_session, tmp_path):
    """One key per burst is what makes overwriting structurally impossible: the
    reviewer harvesting the same task cannot land on the dev's object."""
    first = archive.put(session_key("42", "b1"), a_session)
    (a_session / "projects" / "p" / "sess.jsonl").write_text('{"turn":2}\n', encoding="utf-8")
    second = archive.put(session_key("42", "b2"), a_session)
    assert first != second

    restored = tmp_path / "restored"
    archive.get(session_key("42", "b1"), restored, first)
    assert (restored / "projects" / "p" / "sess.jsonl").read_text() == '{"turn":1}\n'


def test_the_key_scheme_is_the_documented_one():
    """STORES.md's scheme, and A6's composite task id has to survive it."""
    assert session_key("42", "b1") == "sessions/42/b1/session.tar.gz"
    assert session_key("owner/repo#7", "b1") == "sessions/owner/repo#7/b1/session.tar.gz"


# --- the object backend's own behavior ---


def test_the_checksum_rides_as_object_metadata(s3_archive, a_session):
    """"sha256 as object metadata" (A5's scope): readable without downloading the
    object, which is what makes the presigned-PUT path (G2) able to report one."""
    from control_plane.objectstore import CHECKSUM_METADATA_KEY

    checksum = s3_archive.put(KEY, a_session)
    head = s3_archive._client.head_object(Bucket=s3_archive.bucket, Key=s3_archive._object(KEY))
    assert head["Metadata"][CHECKSUM_METADATA_KEY] == checksum
    assert head["ContentLength"] > 0


def test_presigned_urls_read_and_write_one_object(s3_archive, a_session, tmp_path):
    """The capability G2 (#43) is built on: a URL for exactly one object, so a
    burst container needs no storage credential at all. Exercised with a plain
    HTTP client — no signing, no keys — because that is what the container has."""
    import requests

    checksum = s3_archive.put(KEY, a_session)

    got = requests.get(s3_archive.presign_get(KEY, expires_in=60), timeout=30)
    got.raise_for_status()
    assert hashlib.sha256(got.content).hexdigest() == checksum

    upload_key = session_key("42", "presigned")
    body = b"not really a tarball"
    put = requests.put(s3_archive.presign_put(upload_key, expires_in=60), data=body, timeout=30)
    put.raise_for_status()
    assert s3_archive._head(upload_key) is not None
    round_trip = requests.get(s3_archive.presign_get(upload_key, expires_in=60), timeout=30)
    assert round_trip.content == body


def test_a_presigned_url_expires(s3_archive, a_session):
    """Time-boxed to the rail: the URL is not a credential, it is a window."""
    import time

    import requests

    s3_archive.put(KEY, a_session)
    url = s3_archive.presign_get(KEY, expires_in=1)
    time.sleep(2)
    assert requests.get(url, timeout=30).status_code == 403


# --- the URL that chooses a backend ---


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "s3://key:secret@localhost:8333/bucket?tls=false",
            {"endpoint": "http://localhost:8333", "bucket": "bucket", "prefix": ""},
        ),
        (
            "s3://key:secret@objects.example.com/sessions-bucket/platform",
            {
                "endpoint": "https://objects.example.com",
                "bucket": "sessions-bucket",
                "prefix": "platform",
            },
        ),
        (
            "s3://objects.example.com/b",  # credentials may come from elsewhere
            {"endpoint": "https://objects.example.com", "bucket": "b", "prefix": ""},
        ),
    ],
)
def test_archive_url_parses(url, expected):
    """One string per store, like the ledger's DSN: endpoint, credentials, bucket
    and optional prefix. TLS is on unless the URL says otherwise — `tls=false` is
    this store's `sslmode=disable`, for a rig that speaks plain HTTP."""
    from control_plane.objectstore import ObjectSessionArchive

    archive = ObjectSessionArchive.from_url(url)
    assert archive.bucket == expected["bucket"]
    assert archive.prefix == expected["prefix"]
    assert archive._client.meta.endpoint_url == expected["endpoint"]


def test_archive_url_percent_decodes_a_secret():
    """A secret containing @ or / has to be percent-encoded to survive a URL, and
    has to arrive at the client decoded."""
    from control_plane.objectstore import ObjectSessionArchive

    archive = ObjectSessionArchive.from_url("s3://key:se%2Fcre%40t@host/bucket")
    credentials = archive._client._request_signer._credentials
    assert credentials.secret_key == "se/cre@t"


@pytest.mark.parametrize(
    ("url", "complaint"),
    [
        ("postgresql://host/db", "must be an s3:// URL"),
        ("s3://host", "names no bucket"),
        ("s3:///bucket", "no host"),
    ],
)
def test_a_malformed_archive_url_says_what_it_wanted(url, complaint):
    from control_plane.objectstore import ObjectSessionArchive

    with pytest.raises(ArchiveError, match=complaint):
        ObjectSessionArchive.from_url(url)


def test_open_archive_picks_by_presence(tmp_path, monkeypatch):
    """The toggle, and the only place a backend is chosen."""
    from control_plane.store import open_archive

    monkeypatch.delenv(ARCHIVE_URL_ENV, raising=False)
    local = open_archive(tmp_path)
    assert isinstance(local, LocalSessionArchive)
    # rooted at the data dir: the keys carry `sessions/` themselves, so the local
    # tree and a bucket hold the same paths
    assert local.root == tmp_path
    local.put(KEY, tmp_path)
    assert (tmp_path / "sessions" / "42" / "b1" / "session.tar.gz").is_file()

    monkeypatch.setenv(ARCHIVE_URL_ENV, "s3://key:secret@localhost:8333/bucket?tls=false")
    from control_plane.objectstore import ObjectSessionArchive

    assert isinstance(open_archive(tmp_path), ObjectSessionArchive)


def test_the_archive_url_password_never_reaches_the_log():
    """`open_archive` logs which backend it picked, the same way `open_ledger`
    does — and through the same redaction."""
    from control_plane.store import _redact

    assert _redact("s3://platform:platform-dev-secret@localhost:8333/sessions") == (
        "s3://platform:***@localhost:8333/sessions"
    )


def test_a_local_archive_lays_its_keys_out_as_paths(tmp_path, a_session):
    """Backend-specific, and the reason the single-node variant needs no server:
    the key *is* the path, so the tree is visible with `ls`, and the checksum sits
    beside the object where object metadata would be."""
    archive = LocalSessionArchive(tmp_path)
    checksum = archive.put(KEY, a_session)

    blob = tmp_path / "sessions" / "42" / "b1" / "session.tar.gz"
    assert blob.is_file()
    assert blob.with_suffix(".gz.sha256").read_text() == checksum
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == checksum
