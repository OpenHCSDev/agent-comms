"""Local same-UID, read-only recovery snapshot gateway (not an ACP endpoint).

The trusted caller configures one existing wire root. This service is deliberately
independent of a thread's Pi owner and has no production listener/auto-start.
Same-UID peer credentials authorize the local OS-user boundary only: they do
not identify a human, isolate agents of that UID, or authorize remote clients.
"""

from __future__ import annotations

import asyncio
import errno

try:
    import fcntl
except ImportError:  # unsupported OS: start() fails closed
    fcntl = None  # type: ignore[assignment]
import json
import os
import socket
import sqlite3
import stat
import struct
import sys
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_comms.coordination_tables.executions import ExecutionRecord
from agent_comms.coordination_tables.participants import OwnerGenerations

from .child_process import BoundedRun, ParentLifeline
from .field_codec import FieldCodec
from .coordination_database import CoordinationStore
from .recovery_projection import RecoveryRequest, RecoverySelection
from .typed_table import SQLiteJournalMode
from .private_path import PrivateSocketRole

_MAX_REQUEST = 1024
_MAX_REPLY = 4096
_MAX_OWNER_EXECUTIONS = 256
_MAX_REGISTERED_OWNERS = 256
_READ_TIMEOUT = 1.0
_MAX_CLIENTS = 8
_ERROR = b'{"schema":1,"availability":"unavailable","reason":"gateway_unavailable"}\n'


class GatewayUnavailableError(RuntimeError):
    """Gateway admission failed without exposing a private path or SQL detail."""


def _owned(path: Path, kind: int, mode: int | None = None) -> os.stat_result:
    info = path.lstat()  # refuse a symlink as the final component
    if stat.S_IFMT(info.st_mode) != kind or info.st_uid != os.geteuid():
        raise GatewayUnavailableError("local gateway path has invalid ownership or type")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise GatewayUnavailableError("local gateway path has invalid permissions")
    return info


def _peer_uid(sock: socket.socket) -> int:
    """Kernel evidence, never a claimed UID inside the request."""
    if sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
        _pid, uid, _gid = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        )
        return int(uid)
    if sys.platform == "darwin":
        # asyncio's TransportSocket exposes dup(), but not getpeereid().
        with closing(sock.dup()) as peer:
            if not hasattr(peer, "getpeereid"):
                raise GatewayUnavailableError("kernel peer credentials are unavailable")
            uid, _gid = peer.getpeereid()
            return int(uid)
    raise GatewayUnavailableError("kernel peer credentials are unavailable")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode_request(raw: bytes) -> str:
    if len(raw) > _MAX_REQUEST or raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
        raise ValueError("one bounded line is required")
    value = json.loads(
        raw.decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicate_keys
    )
    return FieldCodec.decode(RecoveryRequest, value).thread


def _validate_paths(root: Path, database: Path) -> None:
    # Path.absolute() does not resolve symlinks. Refuse every component actually
    # traversed, not only the configured root's final component.
    for component in (root, *root.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise GatewayUnavailableError("trusted root contains a symlink component")
        if not stat.S_ISDIR(info.st_mode):
            raise GatewayUnavailableError("trusted root contains a non-directory component")
        if info.st_uid not in (0, os.geteuid()):
            raise GatewayUnavailableError("trusted root ancestor has foreign ownership")
        # A root/service-owned sticky /var/tmp protects an owned child from
        # other UIDs; a nonsticky writable ancestor can be replaced underneath us.
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise GatewayUnavailableError("trusted root ancestor is replaceable")
    _owned(root, stat.S_IFDIR)
    if root.stat().st_mode & 0o022:
        raise GatewayUnavailableError("trusted root must not be group/other writable")
    _owned(database, stat.S_IFREG, 0o600)
    with database.open("rb") as stream:
        header = stream.read(100)
    if len(header) != 100 or header[:16] != b"SQLite format 3\0" or header[18:20] != b"\x01\x01":
        raise GatewayUnavailableError("coordinator must be an existing rollback-journal database")


def _snapshot(root: Path, database: Path, requested: str) -> bytes:
    """Resolve identity under one read lock, then recheck it inside the frozen reader.

    The SAME rollback-journal read transaction owns the bounded identity checks
    and projection. No second connection can wait behind a writer attempting
    COMMIT while this original reader still holds its snapshot.
    """
    _validate_paths(root, database)
    with CoordinationStore.observing(database, lock_timeout=0.25) as db:
        if SQLiteJournalMode.read(db.execute("PRAGMA journal_mode")) != [
            SQLiteJournalMode("delete")
        ]:
            raise GatewayUnavailableError("unsupported coordinator journal")
        # owner_thread has no index: cap the entire registered-owner
        # cardinality before the exact-match join can scan it.
        owners = OwnerGenerations.read(
            db.execute(
                "SELECT * FROM owner_generations LIMIT ?",
                (_MAX_REGISTERED_OWNERS + 1,),
            )
        )
        if len(owners) > _MAX_REGISTERED_OWNERS:
            raise GatewayUnavailableError("registered owner scan exceeds budget")
        # Exact current canonical name only. Aliases, claims of lookup, and
        # registration of a human participant are not accepted.
        matches = OwnerGenerations.read(
            db.execute(
                "SELECT g.* FROM owner_generations g "
                "JOIN participants p ON p.participant_lookup=g.owner_lookup "
                "WHERE g.owner_thread=? AND p.committed=1 LIMIT 2",
                (requested,),
            )
        )
        if len(matches) != 1:
            raise GatewayUnavailableError("unknown or ambiguous owner")
        owner = matches[0]
        if owner.owner_thread != requested:
            raise GatewayUnavailableError("invalid canonical owner")
        # The declared owner/status index bounds the frozen reader's scan.
        count = ExecutionRecord.read(
            db.execute(
                "SELECT * FROM executions WHERE owner_lookup=? LIMIT ?",
                (owner.owner_lookup, _MAX_OWNER_EXECUTIONS + 1),
            )
        )
        if len(count) > _MAX_OWNER_EXECUTIONS:
            raise GatewayUnavailableError("owner projection exceeds bounded scan")
        result = RecoverySelection.project(db, owner.owner_lookup, owner.owner_thread)
        encoded = (json.dumps(FieldCodec.encode(result), separators=(",", ":")) + "\n").encode()
        if len(encoded) > _MAX_REPLY:
            raise GatewayUnavailableError("projection exceeds bounded response")
        return encoded


@dataclass(frozen=True)
class SnapshotInvocation:
    root: str
    requested: str

    def run(self) -> bytes:
        root = Path(self.root)
        try:
            return _snapshot(root, root / "coordination.sqlite3", self.requested)
        except Exception:
            return _ERROR


class RecoveryGateway:
    """Explicitly started local service; production integration is a separate gate."""

    def __init__(self, trusted_root: Path):
        self.root = Path(trusted_root).expanduser().absolute()
        self.database = self.root / "coordination.sqlite3"
        self.directory = self.root / ".recovery-viewer"
        self.path = self.directory / "gateway.sock"
        self._server: asyncio.AbstractServer | None = None
        self._lock_fd: int | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._clients = asyncio.Semaphore(_MAX_CLIENTS)
        self._worker_slots = asyncio.Semaphore(_MAX_CLIENTS)
        self._workers: set[asyncio.Task[bytes]] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._closing = False
        self._orphaned = False

    def _prepare_directory(self) -> None:
        if fcntl is None or os.getuid() != os.geteuid():
            raise GatewayUnavailableError("gateway platform or privileges unsupported")
        _validate_paths(self.root, self.database)
        with suppress(FileExistsError):
            self.directory.mkdir(mode=0o700)
        _owned(self.directory, stat.S_IFDIR, 0o700)

    def _lock_instance(self) -> None:
        lock_path = self.directory / "gateway.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise GatewayUnavailableError("invalid gateway lock")
            _owned(lock_path, stat.S_IFREG, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
        except BaseException:
            os.close(fd)
            raise

    def _remove_stale_socket(self) -> None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise GatewayUnavailableError("foreign socket entry")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise GatewayUnavailableError("socket permissions are not private")
        with closing(socket.socket(socket.AF_UNIX)) as probe:
            probe.settimeout(0.1)
            with PrivateSocketRole.address(self.path) as address:
                error = probe.connect_ex(str(address))
            if error == 0:
                raise GatewayUnavailableError("another listener already owns the socket")
            if error != errno.ECONNREFUSED:
                raise GatewayUnavailableError("socket liveness could not be proved")
        current = self.path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise GatewayUnavailableError("socket identity changed")
        self.path.unlink()

    async def start(self) -> None:
        if self._server is not None or self._lock_fd is not None or self._orphaned:
            raise GatewayUnavailableError("gateway already started or not fully closed")
        self._closing = False
        if not (
            (sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"))
            or (sys.platform == "darwin" and hasattr(socket.socket, "getpeereid"))
        ):
            raise GatewayUnavailableError("peer credential platform unsupported")
        self._prepare_directory()
        try:
            self._lock_instance()
            self._remove_stale_socket()
            # This class must run in a dedicated gateway process: umask is
            # process-wide. Pre-bind mode 0600, never chmod a public socket.
            old_mask = os.umask(0o177)
            try:
                bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    with PrivateSocketRole.address(self.path) as address:
                        bound.bind(str(address))
                    info = self.path.lstat()
                    self._socket_identity = (info.st_dev, info.st_ino)
                    if (
                        not stat.S_ISSOCK(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600
                    ):
                        raise GatewayUnavailableError("socket was not created private")
                    bound.listen(_MAX_CLIENTS)
                    bound.setblocking(False)
                except BaseException:
                    bound.close()
                    raise
            finally:
                os.umask(old_mask)
            # Restore the process-wide umask *before* yielding to the loop.
            try:
                self._server = await asyncio.start_unix_server(
                    self._handle, sock=bound, limit=_MAX_REQUEST + 1
                )
            except BaseException:
                bound.close()
                raise
        except BaseException:
            await self.close()
            raise

    async def _run_snapshot_process(self, requested: str) -> bytes:
        """A12 owns deadline, parent loss, process group retirement and reap."""
        invocation = SnapshotInvocation(str(self.root), requested)
        with ParentLifeline() as life:
            try:
                result = await BoundedRun.run(
                    (
                        sys.executable,
                        "-m",
                        "agent_comms.recovery_gateway",
                        json.dumps(FieldCodec.encode(invocation)),
                    ),
                    timeout=_READ_TIMEOUT,
                    env=life.environment,
                    pass_fds=(life.read_fd,),
                )
            except (RuntimeError, TimeoutError):
                # A12 could not attest retirement. Keep the reader admission
                # and single-instance lock until this gateway process exits.
                self._orphaned = self._closing = True
                raise
        response = result.stdout
        if (
            not result.outcome.successful
            or not response.endswith(b"\n")
            or len(response) > _MAX_REPLY
        ):
            raise GatewayUnavailableError("snapshot child failed or exceeded reply bound")
        return response

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handler = asyncio.current_task()
        if handler is not None:
            self._handlers.add(handler)
        try:
            sock = writer.get_extra_info("socket")
            if self._closing or sock is None or _peer_uid(sock) != os.geteuid():
                return  # credential check precedes all request parsing and SQL
            if self._clients.locked():
                return
            async with self._clients:
                deadline = asyncio.get_running_loop().time() + _READ_TIMEOUT
                data = bytearray()
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise ValueError("request deadline")
                    chunk = await asyncio.wait_for(
                        reader.read(min(512, _MAX_REQUEST + 1 - len(data))), timeout=remaining
                    )
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > _MAX_REQUEST:
                        raise ValueError("request too large")
                requested = _decode_request(bytes(data))
                if self._closing or self._orphaned or self._worker_slots.locked():
                    raise GatewayUnavailableError("snapshot workers are unavailable")
                await self._worker_slots.acquire()
                work = asyncio.create_task(self._run_snapshot_process(requested))
                self._workers.add(work)

                def finished(future: asyncio.Task[bytes]) -> None:
                    self._workers.discard(future)
                    if not self._orphaned:
                        self._worker_slots.release()
                    if not future.cancelled():
                        future.exception()  # consume a detached child failure

                work.add_done_callback(finished)
                # The child has its own deadline and OS kill/reap. A client
                # timeout cannot cancel cleanup or leave SQLite locks behind.
                response = await asyncio.wait_for(asyncio.shield(work), timeout=_READ_TIMEOUT)
                writer.write(response)
                await asyncio.wait_for(writer.drain(), timeout=_READ_TIMEOUT)
        except (
            OSError,
            sqlite3.Error,
            struct.error,
            ValueError,
            RecursionError,
            GatewayUnavailableError,
            TimeoutError,
        ):
            try:
                writer.write(_ERROR)
                await asyncio.wait_for(writer.drain(), timeout=_READ_TIMEOUT)
            except (OSError, TimeoutError):
                pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            if handler is not None:
                self._handlers.discard(handler)

    async def close(self) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # Do not release the single-instance lock or unlink the endpoint while
        # detached reads can still hold a rollback-journal snapshot.
        for active in (self._handlers, self._workers):
            if active:
                _done, pending = await asyncio.wait(active, timeout=2 * _READ_TIMEOUT)
                if pending:
                    raise GatewayUnavailableError("gateway still has active snapshot work")
        if self._orphaned:
            raise GatewayUnavailableError("snapshot retirement was not attested")
        self._orphaned = False
        self._worker_slots = asyncio.Semaphore(_MAX_CLIENTS)
        if self._socket_identity is not None:
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self._socket_identity and stat.S_ISSOCK(
                    info.st_mode
                ):
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self._socket_identity = None
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None


if __name__ == "__main__":
    ParentLifeline.guard()
    invocation = FieldCodec.decode(SnapshotInvocation, json.loads(sys.argv[1]))
    sys.stdout.buffer.write(invocation.run())
    sys.stdout.buffer.flush()
