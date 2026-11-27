"""Shared multiplexed Antigravity ACP runtime and WAL-safe on-disk session storage.

One ``agy acp --dangerously-skip-permissions`` process serves every Antigravity thread
inside a worker concurrently by demuxing JSON-RPC responses by request ``id`` and
``session/update`` notifications by ``params.sessionId``. Session history lives in
Antigravity's native SQLite + annotations + brain directories under
``~/.gemini/antigravity-cli``, enabling atomic lossless forks via SQLite backup and
``trajectory_meta`` identity reassignment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import uuid
from abc import ABC, abstractmethod
from asyncio import Future, Lock, Queue, StreamReader, StreamWriter, Task
from asyncio.subprocess import PIPE, Process
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self
from weakref import WeakKeyDictionary

from pydantic import Field, JsonValue, ValidationError

from agent_comms.config_option import CatalogChoice
from agent_comms.execution.antigravity_acp_events import (
    AcpConfigKey,
    AcpConfigOptionKind,
    AcpConfigOptionWire,
    AcpDescriptorBuilder,
    AcpPromptFrame,
    AcpResultFrame,
    AcpUpdateFrame,
    AcpWireModel,
    AntigravityAcpUpdate,
)
from agent_comms.execution.child_process import NoProcess, Platform, ProcessIdentity, ProcessState
from agent_comms.execution.native_session_files import SessionFiles
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import AcpSessionId, NominalText
from agent_comms.storage.store_files import AtomicFile

DEFAULT_ANTIGRAVITY_HOME: Path = Path(".gemini/antigravity-cli")
logger = logging.getLogger(__name__)


class AntigravityConversationId(NominalText):
    """Nominal identifier for one Antigravity conversation database on disk."""


class TranscriptModelLineWire(AcpWireModel):
    """Minimal schema for reading the latest model name from `transcript.jsonl`."""

    model: str = ""


class AcpRpcErrorWire(AcpWireModel):
    """JSON-RPC error payload decoded once at the ACP stdio boundary."""

    code: int = 0
    message: str = "Unknown ACP error"
    data: JsonValue = ""

    def to_exception(self) -> AcpRpcError:
        return AcpRpcError(self.code, self.message, self.data)


class AcpSessionUpdateParamsWire(AcpWireModel):
    """Parameters of an ACP `session/update` notification."""

    session_key: str = Field(default="", alias="sessionId")
    update: dict[str, JsonValue] = Field(default_factory=dict)


class AcpQueueItem(ABC):
    """Item placed onto a per-session ACP update queue."""

    @abstractmethod
    def frames(self) -> tuple[AcpPromptFrame, ...]: ...


@dataclass(frozen=True)
class AcpQueuedUpdate(AcpQueueItem):
    update: AntigravityAcpUpdate

    def frames(self) -> tuple[AcpPromptFrame, ...]:
        return (AcpUpdateFrame(self.update),)


@dataclass(frozen=True)
class AcpQueueClosed(AcpQueueItem):
    def frames(self) -> tuple[AcpPromptFrame, ...]:
        return ()


class AcpNotificationMethod(DeclaredFamily, affix="NotificationMethod"):
    """Nominal dispatch for inbound JSON-RPC notifications from ``agy acp``."""

    @classmethod
    def deliver(
        cls,
        params: Mapping[str, JsonValue],
        session_queues: Mapping[AcpSessionId, Queue[AcpQueueItem]],
    ) -> None:
        del params, session_queues


class SessionUpdateNotificationMethod(AcpNotificationMethod, declared_name="session/update"):
    @classmethod
    def deliver(
        cls,
        params: Mapping[str, JsonValue],
        session_queues: Mapping[AcpSessionId, Queue[AcpQueueItem]],
    ) -> None:
        parsed = AcpSessionUpdateParamsWire.model_validate(params)
        if not parsed.session_key:
            return
        sid = AcpSessionId(parsed.session_key)
        if sid in session_queues:
            session_queues[sid].put_nowait(
                AcpQueuedUpdate(AntigravityAcpUpdate.decode_wire(parsed.update))
            )


class OtherNotificationMethod(AcpNotificationMethod, declared_name="other"):
    pass


class AcpRpcEnvelopeWire(AcpWireModel):
    """Top-level JSON-RPC 2.0 frame emitted by ``agy acp``."""

    id: int = 0
    method: str = ""
    params: dict[str, JsonValue] = Field(default_factory=dict)
    result: JsonValue = Field(default_factory=dict)
    error: AcpRpcErrorWire = Field(default_factory=AcpRpcErrorWire)

    def unwrap_result(self) -> dict[str, JsonValue]:
        if self.error.code != 0:
            raise self.error.to_exception()
        if isinstance(self.result, dict):
            return self.result
        return {}

    def deliver(
        self,
        pending: dict[int, Future[AcpRpcEnvelopeWire]],
        session_queues: Mapping[AcpSessionId, Queue[AcpQueueItem]],
    ) -> None:
        if self.id > 0 and self.id in pending:
            future = pending.pop(self.id)
            if not future.done():
                future.set_result(self)
            return
        if self.method:
            AcpNotificationMethod.member_or_default(self.method, OtherNotificationMethod).deliver(
                self.params, session_queues
            )


class AcpSessionResultWire(AcpWireModel):
    """Result returned by `session/new` or `session/resume`."""

    session_key: str = Field(default="", alias="sessionId")
    config_options: list[AcpConfigOptionWire] = Field(default_factory=list, alias="configOptions")


class AcpPromptResultWire(AcpWireModel):
    """Result returned by `session/prompt`."""

    stop_reason: str = Field(default="", alias="stopReason")


@dataclass(frozen=True)
class AntigravitySessionStorage:
    """Single owner of one Antigravity conversation's durable files on disk."""

    root: Path
    conversation_id: AntigravityConversationId

    TRANSIENT_BRAIN_NAMES: ClassVar[frozenset[str]] = frozenset(
        {
            ".agy-thread.json",
            ".agy-goal-cursor.json",
        }
    )
    TRANSIENT_BRAIN_SUFFIXES: ClassVar[tuple[str, ...]] = (".agy-checkpoint.json",)

    @classmethod
    def default_root(cls) -> Path:
        return Path.home() / DEFAULT_ANTIGRAVITY_HOME

    @classmethod
    def root_for_home(cls, home: Path) -> Path:
        return home / DEFAULT_ANTIGRAVITY_HOME

    @classmethod
    def for_id(cls, conversation_id: AntigravityConversationId) -> Self:
        return cls(root=cls.default_root(), conversation_id=conversation_id)

    @classmethod
    def for_home(cls, conversation_id: AntigravityConversationId, home: Path) -> Self:
        return cls(root=cls.root_for_home(home), conversation_id=conversation_id)

    @classmethod
    def from_db_path(cls, db_path: Path) -> Self:
        resolved = db_path.expanduser().resolve()
        conv = AntigravityConversationId(resolved.stem)
        parent = resolved.parent
        candidate = parent.parent
        if candidate / "conversations" / f"{conv}.db" == resolved:
            return cls(root=candidate, conversation_id=conv)
        return cls(root=parent, conversation_id=conv)

    @property
    def db_path(self) -> Path:
        return self.root / "conversations" / f"{self.conversation_id}.db"

    @property
    def annotations_path(self) -> Path:
        return self.root / "annotations" / f"{self.conversation_id}.pbtxt"

    @property
    def brain_dir(self) -> Path:
        return self.root / "brain" / str(self.conversation_id)

    @property
    def transcript_path(self) -> Path:
        logs = self.brain_dir / ".system_generated/logs"
        full = logs / "transcript_full.jsonl"
        return full if full.is_file() else logs / "transcript.jsonl"

    @property
    def exists(self) -> bool:
        return self.db_path.is_file()

    def last_written(self) -> float:
        mtimes = [
            path.stat().st_mtime for path in (self.db_path, self.transcript_path) if path.is_file()
        ]
        return max(mtimes, default=0.0)

    def latest_reported_model(self) -> str:
        """Read the newest model reported in `transcript.jsonl` without loading `cli_bridges`."""
        path = self.transcript_path
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(max(0, size - 256 * 1024))
            lines = stream.read().splitlines()
        for raw in reversed(lines):
            try:
                record = TranscriptModelLineWire.model_validate_json(raw)
            except ValidationError:
                continue
            model = record.model.strip()
            if model:
                return model
        return ""

    def _ignore_transient_brain(self, directory: str, names: list[str]) -> set[str]:
        del directory
        return {
            name
            for name in names
            if name in self.TRANSIENT_BRAIN_NAMES
            or any(name.endswith(suffix) for suffix in self.TRANSIENT_BRAIN_SUFFIXES)
        }

    def clone_to(self, child_id: AntigravityConversationId) -> AntigravitySessionStorage:
        """Atomically fork this conversation into ``child_id`` with a fresh trajectory identity.

        Uses SQLite's online ``backup()`` API so live WAL-mode conversations fork losslessly
        without pausing the parent session, then rewrites ``trajectory_meta`` so Antigravity's
        ``session/load`` and ``session/resume`` bind the child under ``child_id``.
        """
        if not self.exists:
            raise FileNotFoundError(f"Antigravity conversation database not found: {self.db_path}")
        child = AntigravitySessionStorage(root=self.root, conversation_id=child_id)
        child.db_path.parent.mkdir(parents=True, exist_ok=True)
        child.annotations_path.parent.mkdir(parents=True, exist_ok=True)
        child.brain_dir.parent.mkdir(parents=True, exist_ok=True)
        temp_db = child.db_path.with_suffix(".db.forking")
        try:
            src_conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            dst_conn = sqlite3.connect(temp_db)
            try:
                src_conn.backup(dst_conn)
                new_trajectory = str(uuid.uuid4())
                dst_conn.execute(
                    "UPDATE trajectory_meta SET trajectory_id = ?, cascade_id = ?",
                    (new_trajectory, str(child_id)),
                )
                dst_conn.commit()
            finally:
                dst_conn.close()
                src_conn.close()
            os.replace(temp_db, child.db_path)
        except Exception:
            temp_db.unlink(missing_ok=True)
            raise
        if self.annotations_path.is_file():
            AtomicFile(child.annotations_path).write_bytes(self.annotations_path.read_bytes())
        if self.brain_dir.is_dir():
            shutil.copytree(
                self.brain_dir,
                child.brain_dir,
                ignore=self._ignore_transient_brain,
                dirs_exist_ok=True,
            )
        return child


@dataclass(frozen=True)
class AntigravitySessionFiles(SessionFiles):
    """Lifecycle-owned storage files for one Antigravity conversation."""

    storage: AntigravitySessionStorage
    session: Path
    members: tuple[Path, ...]

    @classmethod
    def for_storage(cls, storage: AntigravitySessionStorage) -> Self:
        db = storage.db_path
        lock = db.with_suffix(".db.lock")
        return cls(
            storage=storage,
            session=db,
            members=(
                db,
                Path(f"{db}-wal"),
                Path(f"{db}-shm"),
                lock,
                storage.annotations_path,
            ),
        )

    @property
    def writer_lock(self) -> Path:
        return self.session.with_suffix(".db.lock")

    def delete(self) -> int:
        freed = super().delete()
        brain = self.storage.brain_dir
        if brain.is_dir():
            for entry in brain.rglob("*"):
                if entry.is_file() or entry.is_symlink():
                    freed += entry.lstat().st_size
            shutil.rmtree(brain)
            Platform.current().sync_directory(brain.parent)
        return freed


@dataclass(frozen=True)
class AcpSessionDescriptor:
    """Metadata returned by ``session/new`` or ``session/resume`` on ``agy acp``."""

    session_id: AcpSessionId
    current_model: str = ""
    thought_level: str = ""
    models: tuple[CatalogChoice, ...] = ()
    thinking_levels: tuple[str, ...] = ()

    @classmethod
    def from_rpc(cls, result: Mapping[str, object]) -> Self:
        wire = AcpSessionResultWire.model_validate(result)
        builder = AcpDescriptorBuilder()
        for option in wire.config_options:
            AcpConfigOptionKind.of(AcpConfigKey(option.id)).apply_to_descriptor(option, builder)
        return cls(
            session_id=AcpSessionId(wire.session_key),
            current_model=builder.current_model,
            thought_level=builder.thought_level,
            models=tuple(builder.models),
            thinking_levels=tuple(builder.thinking_levels),
        )


class AcpRpcError(RuntimeError):
    """Structured JSON-RPC error returned by ``agy acp``."""

    def __init__(self, code: int, message: str, data: object = "") -> None:
        super().__init__(f"Antigravity ACP error {code}: {message}")
        self.code = code
        self.rpc_message = message
        self.data = data


class AcpSubprocessState(ABC):
    """Nominal lifecycle state of the shared ``agy acp`` subprocess."""

    @property
    def alive(self) -> bool:
        return False

    @property
    @abstractmethod
    def identity(self) -> ProcessState: ...

    @abstractmethod
    def require_identity(self) -> ProcessIdentity: ...

    @abstractmethod
    async def write_line(self, encoded: bytes) -> None: ...

    @abstractmethod
    async def terminate(self) -> None: ...


class StoppedAcpSubprocess(AcpSubprocessState):
    """No ``agy acp`` subprocess is running."""

    @property
    def identity(self) -> ProcessState:
        return NoProcess()

    def require_identity(self) -> ProcessIdentity:
        raise ConnectionError("Antigravity ACP server is not running")

    async def write_line(self, encoded: bytes) -> None:
        del encoded
        raise ConnectionError("Antigravity ACP server is not running")

    async def terminate(self) -> None:
        return None


class RunningAcpSubprocess(AcpSubprocessState):
    """One live ``agy acp`` subprocess and its background stdout/stderr readers."""

    def __init__(
        self,
        proc: Process,
        stdin: StreamWriter,
        stdout: StreamReader,
        stderr: StreamReader,
        pid: int,
    ) -> None:
        self._proc = proc
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._pid = pid
        self._running = True
        self._tasks: tuple[Task[None], ...] = ()
        self._stderr_lines: deque[str] = deque(maxlen=64)

    def attach_tasks(self, *tasks: Task[None]) -> None:
        self._tasks = tasks

    def record_stderr(self, line: str) -> None:
        stripped = line.rstrip()
        if stripped:
            self._stderr_lines.append(stripped)

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_lines)[-2048:]

    async def read_stderr_line(self) -> bytes:
        return await self._stderr.readline()

    def mark_exited(self) -> None:
        self._running = False

    @property
    def _has_exited(self) -> bool:
        return isinstance(self._proc.returncode, int)

    @property
    def alive(self) -> bool:
        return self._running and not self._has_exited

    @property
    def identity(self) -> ProcessState:
        return ProcessIdentity.capture(self._pid) if self.alive else NoProcess()

    def require_identity(self) -> ProcessIdentity:
        if not self.alive:
            raise ConnectionError("Antigravity ACP server is not running")
        return ProcessIdentity.capture(self._pid)

    async def readline(self) -> bytes:
        return await self._stdout.readline()

    async def write_line(self, encoded: bytes) -> None:
        if not self.alive:
            raise ConnectionError("Antigravity ACP server is not running")
        self._stdin.write(encoded)
        await self._stdin.drain()

    async def exit_description(self) -> str:
        if not self._has_exited:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=1.0)
            except TimeoutError:
                pass
        rc = self._proc.returncode
        code_text = f"exit code {rc}" if isinstance(rc, int) else "still running"
        tail = self.stderr_tail()
        return f"{code_text}; stderr: {tail}" if tail else code_text

    async def terminate_process(self) -> None:
        self._running = False
        if not self._has_exited:
            with suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self._proc.kill()
                with suppress(ProcessLookupError):
                    await self._proc.wait()

    async def terminate(self) -> None:
        await self.terminate_process()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._tasks = ()


def resolve_agy_binary() -> str:
    """Locate ``agy`` via ``AGENT_COMMS_AGY_BIN``, ``PATH``, or ``~/.local/bin/agy``."""
    override = os.environ.get("AGENT_COMMS_AGY_BIN", "").strip()
    if override:
        return override
    found = shutil.which("agy")
    if found:
        return found
    local_bin = Path.home() / ".local/bin/agy"
    if local_bin.is_file():
        return str(local_bin)
    return "agy"


class AcpLivePrompt:
    """One live ``session/prompt`` turn on ``agy acp`` supporting concurrent mid-turn steers."""

    def __init__(
        self,
        process: AntigravityAcpProcess,
        session_id: AcpSessionId,
        queue: Queue[AcpQueueItem],
        initial_task: Task[dict[str, JsonValue]],
    ) -> None:
        self._process = process
        self._session_id = session_id
        self._queue = queue
        self._in_flight: set[Task[dict[str, JsonValue]]] = {initial_task}
        self._last_stop_reason: str = ""
        self._finished = False

    def steer(self, blocks: Sequence[Mapping[str, object]]) -> None:
        """Inject a mid-turn follow-up prompt concurrently onto the active ``agy acp`` session."""
        if self._finished:
            return
        task = asyncio.create_task(
            self._process._call_locked_unchecked(
                "session/prompt",
                {"sessionId": str(self._session_id), "prompt": list(blocks)},
                timeout=0.0,
            ),
            name=f"antigravity-steer-{str(self._session_id)[:8]}",
        )
        self._in_flight.add(task)

    def _drain_queued_nowait(self) -> list[AcpPromptFrame]:
        frames: list[AcpPromptFrame] = []
        while not self._queue.empty():
            frames.extend(self._queue.get_nowait().frames())
        return frames

    async def next_frames(self) -> tuple[AcpPromptFrame, ...]:
        """Await the next batch of update frames, or the final ``AcpResultFrame`` once all
        in-flight prompt/steer RPCs settle; returns ``()`` after completion."""
        if self._finished:
            return ()
        while True:
            completed = [task for task in self._in_flight if task.done()]
            for task in completed:
                self._in_flight.remove(task)
                result = AcpPromptResultWire.model_validate(await task)
                if result.stop_reason:
                    self._last_stop_reason = result.stop_reason
            if not self._in_flight:
                self._finished = True
                queued = self._drain_queued_nowait()
                queued.append(AcpResultFrame(self._last_stop_reason))
                return tuple(queued)
            if not self._queue.empty():
                return tuple(self._drain_queued_nowait())
            get_task: Task[AcpQueueItem] = asyncio.create_task(self._queue.get())
            wait_set: set[Task[object]] = {*self._in_flight, get_task}
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
            if get_task in done:
                return tuple(get_task.result().frames())
            get_task.cancel()
            with suppress(asyncio.CancelledError):
                await get_task

    async def cancel_and_close(self) -> None:
        self._finished = True
        with suppress(AcpRpcError):
            await self._process.cancel(self._session_id)
        for task in list(self._in_flight):
            task.cancel()
        for task in list(self._in_flight):
            try:
                await task
            except (asyncio.CancelledError, AcpRpcError, ConnectionError):
                pass
        self._in_flight.clear()
        self._process._session_queues.pop(self._session_id, None)

    def close(self) -> None:
        self._finished = True
        self._process._session_queues.pop(self._session_id, None)


class AntigravityAcpProcess:
    """One shared ``agy acp`` process multiplexing arbitrary concurrent sessions over stdio."""

    RPC_TIMEOUT_SECONDS: ClassVar[float] = 60.0

    @classmethod
    def default_command(cls) -> tuple[str, ...]:
        return (
            resolve_agy_binary(),
            "acp",
            "--dangerously-skip-permissions",
            "--disable-telemetry",
        )

    def __init__(
        self,
        command: Sequence[str] = (),
        *,
        environment: Mapping[str, str] = {},
    ) -> None:
        self._command = tuple(command) if command else self.default_command()
        self._environment = dict(environment)
        self._state: AcpSubprocessState = StoppedAcpSubprocess()
        self._start_lock = Lock()
        self._write_lock = Lock()
        self._next_id = 0
        self._pending: dict[int, Future[AcpRpcEnvelopeWire]] = {}
        self._session_queues: dict[AcpSessionId, Queue[AcpQueueItem]] = {}
        self._active_sessions: set[AcpSessionId] = set()
        self._initialized = False

    @property
    def alive(self) -> bool:
        return self._state.alive

    @property
    def process_identity(self) -> ProcessState:
        return self._state.identity

    def require_process_identity(self) -> ProcessIdentity:
        return self._state.require_identity()

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self.alive and self._initialized:
                return
            await self._stop_locked()
            env = self._environment if self._environment else dict(os.environ)
            proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE,
                env=env,
                limit=16 * 1024 * 1024,
            )
            stdin = proc.stdin
            stdout = proc.stdout
            stderr = proc.stderr
            pid = proc.pid
            if not isinstance(stdin, StreamWriter) or not isinstance(stdout, StreamReader):
                raise ConnectionError("Antigravity ACP subprocess missing stdio pipes")
            if not isinstance(stderr, StreamReader) or pid <= 0:
                raise ConnectionError("Antigravity ACP subprocess missing stdio pipes")
            running = RunningAcpSubprocess(proc, stdin, stdout, stderr, pid)
            running.attach_tasks(
                asyncio.create_task(self._read_loop(running), name="antigravity-acp-reader"),
                asyncio.create_task(self._stderr_loop(running), name="antigravity-acp-stderr"),
            )
            self._state = running
            await self._call_locked_unchecked(
                "initialize",
                {"protocolVersion": 1, "clientCapabilities": {}},
                timeout=self.RPC_TIMEOUT_SECONDS,
            )
            self._initialized = True

    async def _stderr_loop(self, running: RunningAcpSubprocess) -> None:
        while True:
            raw = await running.read_stderr_line()
            if not raw:
                return
            running.record_stderr(raw.decode("utf-8", errors="replace"))

    async def _read_loop(self, running: RunningAcpSubprocess) -> None:
        failure_reason = ""
        try:
            while True:
                raw = await running.readline()
                if not raw:
                    break
                stripped = raw.lstrip()
                if not stripped.startswith(b"{"):
                    logger.warning(
                        "Non-JSON stdout from agy acp: %s",
                        stripped[:200].decode("utf-8", errors="replace"),
                    )
                    continue
                envelope = AcpRpcEnvelopeWire.model_validate_json(stripped)
                envelope.deliver(self._pending, self._session_queues)
        except (
            ValidationError,
            json.JSONDecodeError,
            BrokenPipeError,
            ConnectionError,
            EOFError,
        ) as exc:
            failure_reason = f"Antigravity ACP frame error ({type(exc).__name__}): {exc}"
            logger.error("%s", failure_reason)
            await running.terminate_process()
        finally:
            running.mark_exited()
            if not failure_reason:
                description = await running.exit_description()
                failure_reason = f"Antigravity ACP server exited unexpectedly ({description})"
            error = ConnectionError(failure_reason)
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            for queue in list(self._session_queues.values()):
                queue.put_nowait(AcpQueueClosed())
            self._initialized = False

    async def _send_json(self, payload: Mapping[str, object]) -> None:
        encoded = (json.dumps(payload) + "\n").encode("utf-8")
        async with self._write_lock:
            await self._state.write_line(encoded)

    async def _call_locked_unchecked(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float = 0.0,
    ) -> dict[str, JsonValue]:
        self._next_id += 1
        req_id = self._next_id
        future: Future[AcpRpcEnvelopeWire] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._send_json(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            )
            if timeout > 0.0:
                reply = await asyncio.wait_for(future, timeout=timeout)
            else:
                reply = await future
        finally:
            self._pending.pop(req_id, None)
        return reply.unwrap_result()

    async def call(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float = RPC_TIMEOUT_SECONDS,
    ) -> dict[str, JsonValue]:
        await self.ensure_started()
        return await self._call_locked_unchecked(method, params, timeout=timeout)

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        await self.ensure_started()
        await self._send_json({"jsonrpc": "2.0", "method": method, "params": params})

    async def new_session(
        self,
        cwd: Path | str,
        *,
        additional_dirs: Sequence[Path | str] = (),
        system_prompt_append: str = "",
    ) -> AcpSessionDescriptor:
        extras: dict[str, object] = {}
        if additional_dirs:
            extras = {
                **extras,
                "additionalDirectories": [str(Path(d).resolve()) for d in additional_dirs],
            }
        if system_prompt_append:
            extras = {
                **extras,
                "_meta": {"antigravity": {"systemPrompt": {"append": system_prompt_append}}},
            }
        params: dict[str, object] = {
            "cwd": str(Path(cwd).resolve()),
            "mcpServers": [],
            **extras,
        }
        result = await self.call("session/new", params)
        descriptor = AcpSessionDescriptor.from_rpc(result)
        self._active_sessions.add(descriptor.session_id)
        return descriptor

    async def resume_session(
        self,
        session_id: AcpSessionId,
        cwd: Path | str,
        *,
        additional_dirs: Sequence[Path | str] = (),
    ) -> AcpSessionDescriptor:
        extras: dict[str, object] = (
            {"additionalDirectories": [str(Path(d).resolve()) for d in additional_dirs]}
            if additional_dirs
            else {}
        )
        params: dict[str, object] = {
            "sessionId": str(session_id),
            "cwd": str(Path(cwd).resolve()),
            "mcpServers": [],
            **extras,
        }
        result = await self.call("session/resume", params)
        descriptor = AcpSessionDescriptor.from_rpc(result)
        self._active_sessions.add(descriptor.session_id)
        return descriptor

    async def set_config_option(
        self,
        session_id: AcpSessionId,
        config_key: AcpConfigKey,
        value: str,
    ) -> None:
        await self.call(
            "session/set_config_option",
            {"sessionId": str(session_id), "configId": str(config_key), "value": value},
        )

    async def open_prompt(
        self,
        session_id: AcpSessionId,
        blocks: Sequence[Mapping[str, object]],
    ) -> AcpLivePrompt:
        """Start one live ``session/prompt`` turn that accepts mid-turn ``.steer()`` calls."""
        await self.ensure_started()
        queue: Queue[AcpQueueItem] = Queue()
        self._session_queues[session_id] = queue
        prompt_task = asyncio.create_task(
            self._call_locked_unchecked(
                "session/prompt",
                {"sessionId": str(session_id), "prompt": list(blocks)},
                timeout=0.0,
            ),
            name=f"antigravity-prompt-{str(session_id)[:8]}",
        )
        return AcpLivePrompt(self, session_id, queue, prompt_task)

    async def prompt(
        self,
        session_id: AcpSessionId,
        blocks: Sequence[Mapping[str, object]],
    ) -> AsyncIterator[AcpPromptFrame]:
        """Stream decoded `AcpUpdateFrame`s for ``session_id`` and end with `AcpResultFrame`."""
        live = await self.open_prompt(session_id, blocks)
        try:
            while True:
                batch = await live.next_frames()
                if not batch:
                    return
                for frame in batch:
                    yield frame
        except asyncio.CancelledError:
            await live.cancel_and_close()
            raise
        finally:
            live.close()

    async def cancel(self, session_id: AcpSessionId) -> None:
        if not self.alive:
            return
        await self.notify("session/cancel", {"sessionId": str(session_id)})

    async def close_session(self, session_id: AcpSessionId) -> None:
        self._active_sessions.discard(session_id)
        if self.alive:
            with suppress(AcpRpcError):
                await self.call("session/close", {"sessionId": str(session_id)}, timeout=10.0)
        if not self._active_sessions:
            await self.shutdown()

    async def _stop_locked(self) -> None:
        state = self._state
        self._state = StoppedAcpSubprocess()
        self._initialized = False
        await state.terminate()

    async def shutdown(self) -> None:
        async with self._start_lock:
            self._active_sessions.clear()
            await self._stop_locked()


class AntigravityAcpPool:
    """Shares one ``AntigravityAcpProcess`` across every Antigravity session in a worker."""

    _by_runner: ClassVar[WeakKeyDictionary[object, AntigravityAcpProcess]] = WeakKeyDictionary()

    @classmethod
    def for_runner(
        cls,
        runner: object,
        *,
        command: Sequence[str] = (),
    ) -> AntigravityAcpProcess:
        if runner not in cls._by_runner:
            cls._by_runner[runner] = AntigravityAcpProcess(command=command)
        return cls._by_runner[runner]
