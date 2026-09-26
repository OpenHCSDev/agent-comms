"""Hardened creation/connection rules shared by private sidecar stores.

Follows the established private-store pattern (GoalHistoryStore): exclusive
owner-only creation, one serialized schema transaction, ``synchronous=EXTRA``
with post-commit fsync of both file and directory, and verification of the
actual schema objects (not just a stored digest) before any use.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .coordination_store import IdentityConflict

_SYNC_FLAGS = os.O_RDWR | getattr(os, "O_BINARY", 0)


def create_sidecar_file(path: Path, ddl: tuple[tuple[str, str], ...], digest: str) -> None:
    """Create the sidecar with the exact schema in one serialized transaction."""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        pass
    except OSError as error:
        raise IdentityConflict("Cannot create the private sidecar store.") from error
    else:
        os.close(descriptor)
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise IdentityConflict("Private sidecar stores must be owner-only.")
    connection = _configure(sqlite3.connect(path, timeout=5, isolation_level=None))
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ddl[0][0],)
        ).fetchone()
        if exists is None:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for _, statement in ddl:
                    connection.execute(statement)
                connection.execute(f"INSERT INTO {ddl[0][0]} VALUES(1,1,?)", (digest,))
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        _fsync_sidecar(path)
        _verify_schema(connection, ddl, digest)
    finally:
        connection.close()


def _configure(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=EXTRA")
    return connection


def _verify_schema(
    connection: sqlite3.Connection, ddl: tuple[tuple[str, str], ...], digest: str
) -> None:
    actual = {
        row["name"]: row["sql"]
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger','index') "
            f"AND name LIKE '{ddl[0][0].rsplit('_', 1)[0]}_%'"
        )
    }
    if actual != dict(ddl):
        raise IdentityConflict("Sidecar schema objects have drifted.")
    stored = connection.execute(
        f"SELECT version,ddl_digest FROM {ddl[0][0]} WHERE singleton=1"
    ).fetchone()
    if stored is None or tuple(stored) != (1, digest):
        raise IdentityConflict("Sidecar schema version differs.")


def verify_sidecar(path: Path, ddl: tuple[tuple[str, str], ...], digest: str) -> None:
    if not path.exists():
        raise IdentityConflict("Sidecar store is not installed.")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise IdentityConflict("Private sidecar stores must be owner-only.")
    connection = _configure(sqlite3.connect(path, timeout=5, isolation_level=None))
    try:
        _verify_schema(connection, ddl, digest)
    finally:
        connection.close()


def _fsync_sidecar(path: Path) -> None:
    descriptor = os.open(path, _SYNC_FLAGS)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def sidecar_connection(
    path: Path, ddl: tuple[tuple[str, str], ...], digest: str
) -> Iterator[sqlite3.Connection]:
    """Open, verify, and fsync-on-close a private sidecar connection."""
    verify_sidecar(path, ddl, digest)
    connection = _configure(sqlite3.connect(path, timeout=5, isolation_level=None))
    try:
        yield connection
    finally:
        connection.close()
        _fsync_sidecar(path)


def encode_request(text: str) -> str:
    """JSON-encode like the pinned native ``_claimNativeInput`` request."""
    return json.dumps(
        {
            "kind": "prompt",
            "text": text,
            "images": None,
            "streamingBehavior": None,
            "expandPromptTemplates": True,
            "source": "interactive",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def native_request_digest(text: str) -> str:
    """The pinned native input-request digest: sha256 over the encoded request.

    Mirrors ``AgentSession._claimNativeInput`` exactly: the digest covers the
    whole ``pi-input-request-v1`` request envelope, NOT the bare prompt bytes.
    Cross-checked against the real compiled method in tests.
    """
    import hashlib

    return hashlib.sha256(
        ("pi-input-request-v1\n" + encode_request(text)).encode("utf-8")
    ).hexdigest()
