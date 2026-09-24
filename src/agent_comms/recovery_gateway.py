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
import multiprocessing
import os
import socket
import sqlite3
import stat
import struct
import sys
import threading
from collections.abc import Callable
from contextlib import closing, suppress
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

from .coordination import COORDINATION_SCHEMA_VERSION, COORDINATION_SNAPSHOT_VERSION
from .recovery_projection import read_recovery_projection

_MAX_REQUEST = 1024
_MAX_REPLY = 4096
_MAX_OWNER_EXECUTIONS = 256
_MAX_REGISTERED_OWNERS = 256
_READ_TIMEOUT = 1.0
_KILL_GRACE = 2.0
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
    if not isinstance(value, dict) or set(value) != {"thread"}:
        raise ValueError("unexpected request fields")
    thread = value["thread"]
    if (
        not isinstance(thread, str)
        or not 1 <= len(thread) <= 256
        or any(ord(character) < 32 or ord(character) == 127 for character in thread)
    ):
        raise ValueError("invalid thread")
    return thread


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

    Holding the rollback-journal read transaction also prevents a concurrent
    writer committing more owner executions between the bounded precheck and
    the reader's separate atomic read transaction.
    """
    _validate_paths(root, database)
    with closing(
        sqlite3.connect(
            database.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=0.25
        )
    ) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise GatewayUnavailableError("unsupported coordinator journal")
        db.execute("BEGIN")
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != COORDINATION_SCHEMA_VERSION:
                raise GatewayUnavailableError("unsupported coordinator schema")
            meta = db.execute(
                "SELECT schema_version,snapshot_version FROM schema_meta WHERE singleton=1"
            ).fetchone()
            if meta != (COORDINATION_SCHEMA_VERSION, COORDINATION_SNAPSHOT_VERSION):
                raise GatewayUnavailableError("unsupported coordinator snapshot")
            # owner_thread has no v2 index: cap the entire registered-owner
            # cardinality before the exact-match join can scan it.
            owners = db.execute(
                "SELECT owner_lookup FROM owner_generations LIMIT ?",
                (_MAX_REGISTERED_OWNERS + 1,),
            ).fetchall()
            if len(owners) > _MAX_REGISTERED_OWNERS:
                raise GatewayUnavailableError("registered owner scan exceeds budget")
            # Exact current canonical name only. Aliases, claims of lookup, and
            # registration of a human participant are not accepted.
            matches = db.execute(
                "SELECT g.owner_lookup,g.owner_thread FROM owner_generations g "
                "JOIN participants p ON p.participant_lookup=g.owner_lookup "
                "WHERE g.owner_thread=? AND p.committed=1 LIMIT 2",
                (requested,),
            ).fetchall()
            if len(matches) != 1:
                raise GatewayUnavailableError("unknown or ambiguous owner")
            owner_lookup, owner_thread = matches[0]
            if not isinstance(owner_lookup, str) or owner_thread != requested:
                raise GatewayUnavailableError("invalid canonical owner")
            # Index execution_owner_status_idx has owner_lookup as its first
            # column. Bound even the frozen reader's latest-execution scan.
            count = db.execute(
                "SELECT execution_id FROM executions INDEXED BY execution_owner_status_idx "
                "WHERE owner_lookup=? LIMIT ?",
                (owner_lookup, _MAX_OWNER_EXECUTIONS + 1),
            ).fetchall()
            if len(count) > _MAX_OWNER_EXECUTIONS:
                raise GatewayUnavailableError("owner projection exceeds bounded scan")
            result = read_recovery_projection(
                database, owner_lookup=owner_lookup, owner_thread=owner_thread
            )
            encoded = (json.dumps(result.to_primitive(), separators=(",", ":")) + "\n").encode()
            if len(encoded) > _MAX_REPLY:
                raise GatewayUnavailableError("projection exceeds bounded response")
            return encoded
        finally:
            db.execute("ROLLBACK")


def _snapshot_process_entry(
    output: Connection,
    parent_life: Connection,
    root: Path,
    database: Path,
    requested: str,
    snapshot: Callable[[Path, Path, str], bytes],
) -> None:
    """One isolated read. Never send an exception, path, or unbounded payload."""

    def parent_watchdog() -> None:
        # Closing the parent's pipe, including abrupt parent death, terminates
        # this process even if SQLite is blocked in another thread.
        try:
            parent_life.recv_bytes(1)
        except (EOFError, OSError):
            os._exit(1)
        os._exit(1)  # no parent command exists on this one-way lifeline

    threading.Thread(target=parent_watchdog, daemon=True).start()
    try:
        response = snapshot(root, database, requested)
        if (
            not isinstance(response, bytes)
            or len(response) > _MAX_REPLY
            or not response.endswith(b"\n")
        ):
            response = _ERROR
    except BaseException:
        response = _ERROR
    try:
        output.send_bytes(response)
    except (BrokenPipeError, OSError):
        pass
    finally:
        output.close()
        parent_life.close()


async def _readable(fd: int, deadline: float) -> None:
    """Await a POSIX pipe/process sentinel without blocking an event-loop thread."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()

    def signal() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_reader(fd, signal)
    try:
        await asyncio.wait_for(ready, timeout=max(0, deadline - loop.time()))
    finally:
        loop.remove_reader(fd)


async def _read_child_reply(input_pipe: Connection, deadline: float) -> bytes:
    raw = bytearray()
    fd = input_pipe.fileno()
    while True:
        await _readable(fd, deadline)
        chunk = os.read(fd, max(1, _MAX_REPLY + 5 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > _MAX_REPLY + 4:
            raise GatewayUnavailableError("snapshot child reply exceeds bound")
    if len(raw) < 4:
        raise GatewayUnavailableError("snapshot child omitted reply")
    length = struct.unpack("!i", raw[:4])[0]
    if length <= 0 or length > _MAX_REPLY or len(raw) != length + 4:
        raise GatewayUnavailableError("snapshot child reply is malformed")
    response = bytes(raw[4:])
    if not response.endswith(b"\n"):
        raise GatewayUnavailableError("snapshot child reply is unterminated")
    value = json.loads(response.decode("utf-8", errors="strict"))
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise GatewayUnavailableError("snapshot child reply is invalid")
    return response


async def _reap_child(process: BaseProcess, deadline: float) -> None:
    if process.exitcode is None:
        await _readable(process.sentinel, deadline)
    # Sentinel readiness can race the waitpid observation by one scheduler
    # tick; make a short bounded reap attempt before declaring an orphan.
    process.join(timeout=0.05)
    if process.exitcode is None:
        raise GatewayUnavailableError("snapshot child could not be reaped")


class RecoveryGateway:
    """Explicitly started local service; production integration is a separate gate."""

    def __init__(
        self,
        trusted_root: Path,
        *,
        _snapshot_function: Callable[[Path, Path, str], bytes] = _snapshot,
    ):
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
        self._processes: set[BaseProcess] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._closing = False
        self._orphaned = False
        # A private service-only injection allows real-lock child tests; never
        # sourced from a request or exposed as a production listener option.
        self._snapshot_function = _snapshot_function

    def _prepare_directory(self) -> None:
        if fcntl is None or os.getuid() != os.geteuid():
            raise GatewayUnavailableError("gateway platform or privileges unsupported")
        _validate_paths(self.root, self.database)
        if len(os.fsencode(self.path)) >= 100:
            raise GatewayUnavailableError("socket path is too long; no /tmp fallback")
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
            error = probe.connect_ex(str(self.path))
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
                    bound.bind(str(self.path))
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
        """Kill and reap the *SQLite process* on timeout or cancellation."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _READ_TIMEOUT
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        life_child, life_parent = context.Pipe(duplex=False)
        process = context.Process(
            target=_snapshot_process_entry,
            args=(send, life_child, self.root, self.database, requested, self._snapshot_function),
            daemon=True,
        )
        try:
            process.start()
        except BaseException as error:
            receive.close()
            send.close()
            life_child.close()
            life_parent.close()
            raise GatewayUnavailableError("snapshot child could not start") from error
        send.close()
        life_child.close()
        self._processes.add(process)
        try:
            response = await _read_child_reply(receive, deadline)
            await _reap_child(process, deadline)
            if process.exitcode != 0:
                raise GatewayUnavailableError("snapshot child failed")
            return response
        finally:
            receive.close()
            life_parent.close()
            if process.exitcode is None:
                with suppress(OSError):
                    process.kill()
                try:
                    await _reap_child(process, loop.time() + _KILL_GRACE)
                except (OSError, TimeoutError, GatewayUnavailableError):
                    # Never free a worker slot or single-instance lock while a
                    # child might still own SQLite read locks. Supervisor exit
                    # is the final recovery boundary for an unkillable child.
                    self._orphaned = True
                    self._closing = True
            if process.exitcode is not None:
                process.close()
                self._processes.discard(process)

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
        for process in tuple(self._processes):
            if process.exitcode is not None:
                process.join(timeout=0)
                process.close()
                self._processes.discard(process)
        if self._processes:
            raise GatewayUnavailableError("gateway still owns an unreaped snapshot child")
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
