"""The S3 session archive — the same key-addressed protocol, on a bucket.

`LocalSessionArchive` and this class are one design with and without a network,
not two modes (STORES.md). Callers hold a `SessionArchive` and cannot tell which
they have; gate A7 (#18) is that claim made about the whole MVP scenario.

**S3 the protocol, not a vendor.** The rig runs SeaweedFS, a deployment might buy
managed object storage, and the code says neither: it speaks S3 with path-style
addressing and SigV4, which is what every implementation of it accepts
(capabilities, not instruments — TO-BE §12). `boto3` is here as the reference
client for that protocol, the same way `psycopg` is here for Postgres' — the
alternative is hand-rolling request signing, which is a worse bet than a
dependency.

**Write-once, checked before the write.** S3 has no portable compare-and-set, so
`put` heads the key first and refuses if anything is there. The race that leaves
open — two writers, same key, same instant — is unreachable by construction: a
key contains a burst id, and a burst id is minted once at spawn. A retry is a new
burst, which is a new key, which is the point of the tree.

**The checksum travels twice.** It goes in the ledger, which is the authority, and
into object metadata, which is not. The metadata copy exists for the case where
the control plane never touches the bytes: with presigned PUT (G2, #43) the
harvester uploads directly, and `recorded_checksum` is how the number gets home.

Credentials stay in the control plane. Nothing in a burst container ever holds
them — at G2 the container gets a URL for one object, minted here, expiring inside
its own rail.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import structlog

from control_plane.store import ArchiveError, tar_without_credentials, untar

log = structlog.get_logger()

# Where the sha256 rides on the object. `x-amz-meta-` is added by S3 itself; the
# key below is what the protocol calls user metadata.
CHECKSUM_METADATA_KEY = "sha256"

# Every S3 implementation wants a region; only some care what it says. This is the
# value that means "unset" to the ones that don't.
DEFAULT_REGION = "us-east-1"


class ObjectSessionArchive:
    """The bucket-backed archive. Same protocol `LocalSessionArchive` implements."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        prefix: str = "",
        region: str = DEFAULT_REGION,
    ) -> None:
        import boto3  # local: keeps boto3 off the import path of every CLI verb
        from botocore.config import Config

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                # path style, always: virtual-host addressing needs per-bucket DNS,
                # which a single-node gateway or an IP endpoint cannot provide.
                s3={"addressing_style": "path"},
                retries={"mode": "standard", "max_attempts": 3},
            ),
        )
        self._bucket_ensured = False

    @classmethod
    def from_url(cls, url: str) -> ObjectSessionArchive:
        """Parse `ARCHIVE_URL` — the archive's twin of `LEDGER_DSN`.

            s3://access_key:secret_key@host:port/bucket[/prefix][?tls=false]

        One string carries endpoint, credentials and bucket, for the same reason
        the ledger's does: a deployment configures one variable per store, and
        there is one place to redact when it gets logged. `tls=false` is the
        archive's `sslmode=disable` — the dev rig speaks plain HTTP.
        """
        parsed = urlparse(url)
        if parsed.scheme != "s3":
            raise ArchiveError(f"ARCHIVE_URL must be an s3:// URL, got {parsed.scheme or url!r}://")
        if not parsed.hostname:
            raise ArchiveError("ARCHIVE_URL has no host — expected s3://[key:secret@]host/bucket")

        path = parsed.path.lstrip("/")
        bucket, _, prefix = path.partition("/")
        if not bucket:
            raise ArchiveError("ARCHIVE_URL names no bucket — expected s3://host/bucket")

        options = parse_qs(parsed.query)
        tls = options.get("tls", ["true"])[0].lower() not in ("false", "0", "no")
        port = f":{parsed.port}" if parsed.port else ""
        return cls(
            bucket,
            endpoint_url=f"{'https' if tls else 'http'}://{parsed.hostname}{port}",
            # a secret in a URL must percent-encode @ and /, so decode both
            access_key=unquote(parsed.username or ""),
            secret_key=unquote(parsed.password or ""),
            prefix=prefix,
            region=options.get("region", [DEFAULT_REGION])[0],
        )

    # --- keys ---

    def _object(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _ensure_bucket(self) -> None:
        """Create the bucket if the deployment hasn't. Once per process, lazily —
        a `stop` or a `migrate` should not pay for a network round trip, and a
        credential without the right fails here rather than mid-harvest."""
        if self._bucket_ensured:
            return
        from botocore.exceptions import ClientError

        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            if _status(exc) not in (403, 404):
                raise
            if _status(exc) == 404:
                self._client.create_bucket(Bucket=self.bucket)
                log.info("archive bucket created", bucket=self.bucket)
        self._bucket_ensured = True

    # --- the protocol ---

    def put(self, key: str, session_dir: Path) -> str:
        self._ensure_bucket()
        if self._head(key) is not None:
            raise ArchiveError(f"snapshots are write-once and {key} already exists")

        with tempfile.TemporaryDirectory() as staging:
            blob = Path(staging) / "session.tar.gz"
            checksum = tar_without_credentials(session_dir, blob)
            with blob.open("rb") as body:
                self._client.put_object(
                    Bucket=self.bucket,
                    Key=self._object(key),
                    Body=body,
                    Metadata={CHECKSUM_METADATA_KEY: checksum},
                )
        return checksum

    def get(self, key: str, dest_dir: Path, expected_sha256: str) -> None:
        from botocore.exceptions import ClientError

        with tempfile.TemporaryDirectory() as staging:
            blob = Path(staging) / "session.tar.gz"
            try:
                self._client.download_file(self.bucket, self._object(key), str(blob))
            except ClientError as exc:
                if _status(exc) == 404:
                    raise ArchiveError(f"no snapshot at {key}") from exc
                raise
            actual = hashlib.sha256(blob.read_bytes()).hexdigest()
            if actual != expected_sha256:
                # Deliberately before extraction: bytes the ledger doesn't vouch
                # for never reach a volume a burst will mount.
                raise ArchiveError(
                    f"checksum mismatch for {key}: ledger says {expected_sha256}, bytes say {actual}"
                )
            untar(blob, dest_dir)

    def recorded_checksum(self, key: str) -> str | None:
        head = self._head(key)
        return None if head is None else head.get("Metadata", {}).get(CHECKSUM_METADATA_KEY)

    # --- the presigning capability (G2) ---

    def presign_get(self, key: str, expires_in: int) -> str:
        """A URL that reads exactly one object, for exactly that long."""
        return self._presign("get_object", key, expires_in)

    def presign_put(self, key: str, expires_in: int) -> str:
        return self._presign("put_object", key, expires_in)

    def _presign(self, operation: str, key: str, expires_in: int) -> str:
        self._ensure_bucket()
        return self._client.generate_presigned_url(
            operation,
            Params={"Bucket": self.bucket, "Key": self._object(key)},
            ExpiresIn=expires_in,
        )

    # --- internals ---

    def _head(self, key: str) -> dict | None:
        from botocore.exceptions import ClientError

        try:
            return self._client.head_object(Bucket=self.bucket, Key=self._object(key))
        except ClientError as exc:
            if _status(exc) == 404:
                return None
            raise


def _status(exc: Exception) -> int | None:
    """The HTTP status behind a botocore ClientError, or None if it carries none."""
    return getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
