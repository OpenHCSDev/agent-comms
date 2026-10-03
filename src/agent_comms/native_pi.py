"""Pinned Pi RPC execution with durable attempt-bound context evidence.

Native proof records the model context; the coordinator owns disposition and
publication. Verified package and send authority remain required for every turn.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from . import pi_events as pi
from .native_arguments import NativeArguments
from .native_package import OWNER_INSTRUCTIONS
from .owner_launch import RestartEnvironment
from .field_codec import FieldCodec
from .native_input_record import NativeInputCommit, NativeInputIdText
from .native_entries import NativeEntry, NativeEvidenceRead, SessionEntry
from .private_path import FileIdentity, FileRevision, PrivateFileRole, PrivateDirectoryRole, TrustedAncestorRole
from .selected_tool_broker import NativeToolMode
from .typed_table import Column, Index, SQLiteSchemaObject, TypedTable

if TYPE_CHECKING:
    from .fresh_private_session import FreshPrivateSession
    from .selected_session import SelectedSession

CAPABILITY = "pi-native-input-v1-live-only"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# Every tracked launch must remove Pi session retry, provider transport retry,
# and overflow compaction-retry before an input can reach any provider.
_NATIVE_SETTINGS = (
    b'{"retry":{"enabled":false,"maxRetries":0,"provider":{"maxRetries":0}},'
    b'"compaction":{"enabled":false}}\n'
)


class NativePiUnavailable(RuntimeError):  # noqa: N818 - nominal fail-closed outcome
    """Tracked execution failed closed without committing a coordinator fact."""

    native_stderr = ""

    @property
    def diagnostic_evidence(self):
        return {"stderr": self.native_stderr}

    @property
    def public_failure(self):
        return f"{self}; the input is uncertain."

    @property
    def rejected_response(self) -> pi.Response | None:
        return None


class NativePiInputNotSent(NativePiUnavailable):
    """Original admission never entered its prompt writer; grants no replay."""

    def __init__(self, error, attestation):
        self.attestation = attestation
        super().__init__(f"Native Pi failed before prompt admission; input not sent: {error}")

    @property
    def diagnostic_evidence(self):
        return {
            **super().diagnostic_evidence,
            "input_disposition": "not_sent",
            "initialization": self.attestation.diagnostic_evidence,
        }

    @property
    def public_failure(self):
        return str(self)


class NativePiPromptRejected(NativePiUnavailable):
    """A correlated RPC refusal; it grants no retry or admission authority."""

    def __init__(self, response: pi.Response):
        self.response = response
        super().__init__("Native Pi rejected the tracked prompt (see diagnostic)")

    @property
    def rejected_response(self) -> pi.Response:
        return self.response


def main() -> int:
    """Run the active route's pinned Pi for ordinary ACP owner sessions."""
    from .private_nk_entrypoint import PrivateNkLaunch

    launch = PrivateNkLaunch.current()
    if launch is None:
        raise NativePiUnavailable("Native owner backend requires a configured private route")
    cli = launch.validate()
    environment = dict(os.environ)
    argv, environment = NativePiRpcLaunch.bootstrap(
        cli, tuple(sys.argv[1:]), Path.cwd(), environment,
        RestartEnvironment.inherit(environment),
    )
    os.execvpe(argv[0], list(argv), environment)
    return 0


@dataclass(frozen=True, slots=True)
class NativeContextRecord:
    """Native context facts shared by the journal and located evidence."""

    input_id: str = field(
        metadata={
            "wire_name": "inputId",
            "sql": Column(
                primary_key=True, check="length(input_id)=32 AND input_id NOT GLOB '*[^a-f0-9]*'"
            ),
        }
    )
    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_entry_id: str = field(metadata={"wire_name": "sessionEntryId"})
    request_generation: int = field(
        metadata={
            "wire_name": "requestGeneration",
            "sql": Column(
                primary_key=True, check="request_generation BETWEEN 1 AND 9007199254740991"
            ),
        }
    )
    llm_context_digest: str = field(
        metadata={
            "wire_name": "llmContextDigest",
            "sql": Column(
                check="length(llm_context_digest)=64 AND llm_context_digest NOT GLOB '*[^a-f0-9]*'"
            ),
        }
    )

    @classmethod
    def from_events(cls, input_id, session_id, input_event, context_event):
        """Strictly join the two emitted records through the context declaration.

        This is an emitted receipt, not proof of durable inclusion. A journal
        read must independently corroborate the same complete located record.
        """
        record = FieldCodec.decode(cls, {
            item.metadata.get("wire_name", item.name): getattr(context_event, item.name)
            for item in fields(cls)
        })
        if record.input_commit != NativeInputCommit(input_id, session_id, record.session_entry_id):
            raise NativePiUnavailable("Native Pi context names another original input")
        if record.input_commit != NativeInputCommit.from_event(input_event):
            raise NativePiUnavailable("Native Pi input/context events disagree")
        return record

    @property
    def input_commit(self) -> NativeInputCommit:
        return NativeInputCommit(self.input_id, self.session_id, self.session_entry_id)

    def __post_init__(self):
        NativeInputIdText.decode(self.input_id)
        if _DIGEST.fullmatch(self.llm_context_digest) is None:
            raise ValueError("Native context has invalid input or digest identity")
        if not self.session_id or not self.session_entry_id:
            raise ValueError("Native context lacks committed session identity")
        if not 0 < self.request_generation <= 2**53 - 1:
            raise ValueError("Native context generation is outside its native range")

    def at(self, session_file: Path) -> NativeContextProof:
        """Locate recorded facts; this does not grant acceptance or replay."""
        return NativeContextProof(
            self.input_id,
            self.session_id,
            self.session_entry_id,
            self.request_generation,
            self.llm_context_digest,
            session_file,
        )


@dataclass(frozen=True, slots=True)
class NativeContextJournal(NativeContextRecord, TypedTable):
    """One proof declaration owns wire facts and the indexed durable journal."""

    schema: Literal[1]
    type: Literal["context_committed"]

    without_rowid = True
    indexes = (Index(("request_generation",)), Index(("session_id",)))

    @classmethod
    def triggers(cls) -> dict[str, str]:
        table = cls.declared_name
        return {
            f"{table}_append": f"""CREATE TRIGGER {table}_append BEFORE INSERT ON {table}
            WHEN NEW.request_generation < COALESCE((SELECT MAX(request_generation) FROM {table}), 0)
              OR EXISTS(SELECT 1 FROM {table} WHERE request_generation=NEW.request_generation
                        AND llm_context_digest != NEW.llm_context_digest)
              OR NEW.session_id != (SELECT session_id FROM {table} ORDER BY session_id LIMIT 1)
            BEGIN SELECT RAISE(ABORT,'native proof lineage differs'); END""",
            **{
                f"{table}_{action.lower()}": f"CREATE TRIGGER {table}_{action.lower()} "
                f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'native proof is append only'); END"
                for action in ("UPDATE", "DELETE")
            },
        }

    @classmethod
    def native_contract(cls) -> dict[str, Any]:
        """Generate the native writer's SQLite contract from this declaration."""
        table = cls.declared_name
        columns = cls.columns()
        return {
            "objects": cls.schema_objects(),
            "columns": [
                {"name": item.name, "wire": item.metadata.get("wire_name", item.name)}
                for item in fields(cls)
            ],
            "insert": f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            "head": f"SELECT COALESCE(MAX(request_generation),0) AS generation FROM {table}",
            "session": f"SELECT session_id AS id FROM {table} ORDER BY session_id LIMIT 1",
            "current": f"SELECT input_id,session_entry_id FROM {table} "
            f"WHERE request_generation=(SELECT MAX(request_generation) FROM {table})",
        }

    @classmethod
    @contextmanager
    def open_evidence(cls, session_file: Path) -> Iterator[sqlite3.Connection]:
        """Read an indexed snapshot after SQLite's bounded hot-journal recovery.

        A read is corroboration, never a fresh receipt. Keep the private inode
        pinned across SQLite's transaction and refuse redirection or schema drift.
        Open existing storage read/write for SQLite rollback, then forbid SQL
        writes. A cold history view can recover without a model turn or receipt.
        """
        path = Path(str(session_file) + ".input-proof")
        with ExitStack() as custody:
            try:
                held = custody.enter_context(closing(
                    os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
                ))
                before = os.fstat(held.fileno())
                PrivateFileRole.require(before)
                if before.st_nlink != 1:
                    raise NativePiUnavailable("Native proof must be private and unaliased")
                db = custody.enter_context(closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)))
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                actual = SQLiteSchemaObject.read(
                    db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")
                )
                if {item.name: item.sql for item in actual} != cls.schema_objects():
                    raise NativePiUnavailable("Native proof requires offline durable conversion")
                if FileIdentity.from_stat(before) != FileIdentity.from_stat(path.lstat()):
                    raise NativePiUnavailable("Native proof inode changed while opening")
            except (OSError, sqlite3.Error, ValueError, TypeError) as error:
                raise NativePiUnavailable("Native proof indexed evidence is unavailable") from error
            # Consumer exceptions retain their original boundary and disposition.
            # Custody closes the database/descriptor without classifying callers.
            yield db
            try:
                if FileIdentity.from_stat(before) != FileIdentity.from_stat(path.lstat()):
                    raise NativePiUnavailable("Native proof inode changed during observation")
            except OSError as error:
                raise NativePiUnavailable("Native proof indexed evidence is unavailable") from error

    @classmethod
    def for_input(
        cls, db: sqlite3.Connection, input_id: str, generation: int | None = None
    ) -> NativeContextJournal | None:
        """The primary key answers one input without scanning lifetime history."""
        where, parameters = "input_id=?", (input_id,)
        if generation is not None:
            where += " AND request_generation=?"
            parameters += (generation,)
        rows = cls.read(
            db.execute(
                f"SELECT {cls._column_list(cls.columns())} FROM {cls.declared_name} "
                f"WHERE {where} ORDER BY request_generation DESC LIMIT 1",
                parameters,
            )
        )
        return next(iter(rows), None)

    def corroborate(
        self, session_file: Path, header: SessionEntry, tracked: dict[str, NativeEntry]
    ) -> NativeContextProof:
        entry = tracked.get(self.input_id)
        if self.session_id != header.id or entry is None or self.session_entry_id != entry.id:
            raise NativePiUnavailable("Native Pi proof journal source lineage differs")
        return self.at(session_file)


@dataclass(frozen=True, slots=True)
class NativeContextProof(NativeContextRecord):
    session_file: Path

    def corroborates_input(
        self, input_id: str, *, evidence: NativeEvidenceRead | None = None
    ) -> bool:
        """Check an already-observed live event against its isolated saved proof.

        This grants neither replay nor recovery authority. History IO occurs
        before the coordinator read transaction, as on the native return path.
        """
        if self.input_id != input_id:
            return False
        return self.read_evidence(self.session_file, input_id, evidence=evidence) == self

    @classmethod
    def read_evidence(
        cls, session_file: Path, input_id: str, *, request_generation: int | None = None,
        evidence: NativeEvidenceRead | None = None,
    ) -> NativeContextProof:
        """Corroborate live recorded events; parsed bytes alone grant no authority."""
        NativeInputIdText.decode(input_id)
        session_file = Path(session_file).absolute()
        from .native_entries import NativeInputEvidenceRead

        with NativeInputEvidenceRead.borrow(session_file, evidence) as evidence:
            header, entries = evidence.observe()
            tracked = NativeEntry.tracked_users(entries)
            if input_id not in tracked:
                raise NativePiUnavailable("The specified input was never durably committed")
            with NativeContextJournal.open_evidence(session_file) as db:
                row = NativeContextJournal.for_input(db, input_id, request_generation)
                if row is None:
                    raise NativePiUnavailable("The input has no assembled-context proof")
                return row.corroborate(session_file, header, tracked)

    @classmethod
    def read_history_evidence(
        cls, session_file: Path, header: SessionEntry, entries: tuple[NativeEntry, ...],
        *, recorded: tuple[NativeContextProof, ...] = (),
    ) -> dict[str, NativeContextProof]:
        """Corroborate retained inputs using indexed, latest context inclusion.

        This proves historical context inclusion. It never creates an input
        disposition, an owner enrollment, or permission to replay an input.
        """
        tracked = NativeEntry.tracked_users(entries)
        result = {}
        with NativeContextJournal.open_evidence(session_file) as db:
            for input_id in tracked:
                row = NativeContextJournal.for_input(db, input_id)
                if row is not None:
                    result[input_id] = row.corroborate(session_file, header, tracked)
            for proof in recorded:
                if proof.session_file != session_file:
                    raise NativePiUnavailable("Live-recorded context belongs to another session file")
                row = NativeContextJournal.for_input(db, proof.input_id, proof.request_generation)
                if row is None or row.corroborate(session_file, header, tracked) != proof:
                    raise NativePiUnavailable("Live-recorded context differs from native journal")
        return result


class NativePiTerminalFailure(NativePiUnavailable):
    """A proved input ended in a failed terminal and its owned process was reaped."""

    def __init__(self, detail: str, context: NativeContextProof, provider: str, model: str):
        super().__init__(f"Native Pi assistant did not finish successfully: {detail}")
        self.context = context
        self.provider = provider
        self.model = model
        # Provider text may contain arbitrary response bodies. Only complete
        # known short errors may be broadcast into a shared channel.
        match = re.fullmatch(
            r"(?:(?:Codex|OpenAI|Anthropic) error: )?"
            r"(The usage limit has been reached|Insufficient credits|"
            r"(?:401|402|403|429)(?::\s*|\s+)(?:insufficient credits|rate limit|unauthorized)|"
            r"Model output limit reached)\.?",
            detail,
            flags=re.IGNORECASE,
        )
        self.public_message = (
            f"{provider}/{model}: {match.group(1) if match else 'provider request failed'}."
        )


@dataclass(frozen=True, slots=True)
class NativeTurnResult:
    def require_publishable(self) -> None:
        from .coordination_errors import IdentityConflict

        if not self.text:
            raise IdentityConflict("successful model produced no publishable response")

    text: str
    context: NativeContextProof
    selected_tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class NativePiRpcLaunch:
    """Verified native Pi subprocess launch, not an authorization to send input."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    session: SelectedSession
    package: Path

    configuration: RestartEnvironment = field(kw_only=True)

    @classmethod
    def bootstrap(cls, cli, arguments, cwd, environment, configuration: RestartEnvironment):
        """Encode the held launch configuration at the native process boundary."""
        try:
            instructions = OWNER_INSTRUCTIONS.resolve(strict=True)
        except OSError as error:
            raise NativePiUnavailable("Canonical owner instruction asset is unavailable") from error
        env = dict(environment)
        for name in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"):
            env.pop(name, None)
        env["NODE_DISABLE_COMPILE_CACHE"] = "1"
        env["PI_WORKTREE"] = str(cwd)
        env["PATH"] = os.pathsep.join(
            (str(Path(sys.executable).parent), env.get("PATH", os.defpath))
        )
        env.update(configuration.encode_native())
        argv = (
            "node", "--no-global-search-paths",
            "--import", str(cli.with_name("agent-comms-import-fence.mjs")),
            "--import", str(cli.with_name("agent-comms-project-bootstrap.mjs")),
            str(cli), *arguments, "--append-system-prompt", str(instructions),
        )
        return argv, env

    @classmethod
    def _build(cls, cli, arguments, cwd, environment, session, package, configuration):
        argv, env = cls.bootstrap(cli, arguments, cwd, environment, configuration)
        return cls(argv, cwd, env, session, package, configuration=configuration)

    @classmethod
    def _command_selection(cls, command: str):
        """Decode the original launcher selection without acquiring its package.

        Executable names never establish capability. Configured commands must
        name this installation's entrypoint, its pinned CLI, or the source stack
        launcher; execution always uses the verified native package.
        """
        import hashlib

        from .native_package import MANIFEST
        from .private_nk_entrypoint import PrivateNkLaunch

        stack_launcher = MANIFEST.parent / "bin" / "pi-native"
        executable = (
            Path(shutil.which(command) or command).resolve(strict=True) if command != "pi" else None
        )
        route = PrivateNkLaunch.current()
        if route is not None:
            package = route.native_package
        elif executable is not None and executable == stack_launcher.resolve():
            build = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()[:16]
            package = (
                MANIFEST.parent
                / f".pi-native-{build}"
                / "node_modules/@earendil-works/pi-coding-agent"
            )
        else:
            raise NativePiUnavailable("Native owner requires a configured pinned package")
        allowed = (
            Path(sys.executable).with_name("pi-comms-native").resolve(),
            stack_launcher.resolve(),
            (package / "dist" / "cli.js").resolve(),
        )
        if executable is not None and executable not in allowed:
            raise NativePiUnavailable("Configured command is not a validated native Pi launcher")
        return package, route

    @classmethod
    def package_for_command(cls, command: str) -> Path:
        """Acquire the original reviewed artifact before any new native execution."""
        package, route = cls._command_selection(command)
        if route is not None:
            route.validate()
        else:
            _trusted_package(package)
        return package

    @classmethod
    def managed(
        cls,
        command: str,
        arguments: tuple[str, ...],
        *,
        worktree: Path,
        environment: dict[str, str] | None = None,
        session_file: str | None = None,
    ) -> NativePiRpcLaunch:
        """Prepare managed ACP/headless execution; native receipts remain separate."""
        try:
            arguments = NativeArguments.parse(arguments).rpc()
        except ValueError as error:
            raise NativePiUnavailable(str(error)) from error
        package = cls.package_for_command(command)
        cwd = worktree.resolve(strict=True)
        if not cwd.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        from .selected_session import SelectedSession, SavedSelectedSession
        from .native_session_reopen import SessionIdentityHelper

        saved = Path(session_file).absolute() if session_file is not None else None
        directory = saved.parent if saved is not None else cwd
        session = (SavedSelectedSession(directory,
            identity=SessionIdentityHelper.locate(package, str(saved)))
            if saved is not None else SelectedSession(directory))
        return cls._managed(package, arguments, cwd, environment, session)

    @classmethod
    def _managed(cls, package, arguments, cwd, environment, session):
        """Assemble the entire key from one acquired package and selected source."""
        cwd = Path(cwd).resolve(strict=True)
        if not cwd.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        env = dict(os.environ)
        env.update(environment or {})
        if session.path is not None:
            arguments += ("--session", str(session.path))
        cli = package / "dist" / "cli.js"
        return cls._build(cli, arguments, cwd, env, session, package, RestartEnvironment.inherit(env))

    def retained_managed(
        self, command: str, arguments: tuple[str, ...], *, worktree: Path,
        environment: dict[str, str] | None, session_file: str | None,
    ) -> NativePiRpcLaunch | None:
        """Derive a candidate from this acquired immutable launch, not a path cache.

        Only original child custody may consume it after comparing the complete
        launch/auth key and checking its actual saved-source revision/liveness.
        A different package or selected file needs its own fresh acquisition.
        """
        package, _ = self._command_selection(command)
        saved = Path(session_file).absolute() if session_file is not None else None
        if package != self.package or saved != self.session.path:
            return None
        return self._managed(self.package, NativeArguments.parse(arguments).rpc(),
            worktree, environment, self.session)

    @classmethod
    def tracked(
        cls,
        package: Path,
        *,
        worktree: Path,
        session: SelectedSession,
        provider: str = "openrouter",
        model: str = "z-ai/glm-5.3-flash",
        thinking_level: str | None = None,
        environment: dict[str, str] | None = None,
        selected_tool_mode: NativeToolMode | None = None,
    ) -> NativePiRpcLaunch:
        """Verify compiled Pi bytes and commit private no-retry policy before spawning.

        A backend must explicitly consume this launch, not guess Pi from a basename
        or trust an RPC capability response from an arbitrary executable. This
        only establishes the executable and its settings; native input, context,
        and model-delivery proofs remain separate per-attempt observations.
        """
        session_dir, session_file = session.directory, session.path
        try:
            for value in (provider, model):
                FieldCodec.decode(str, value)
                if re.fullmatch(r"[^\s-]\S*", value) is None:
                    raise ValueError("Provider/model must be single non-option tokens")
        except (TypeError, ValueError) as error:
            raise NativePiUnavailable("Native Pi requires an explicit provider and model") from error
        if selected_tool_mode is not None and not isinstance(selected_tool_mode, NativeToolMode):
            raise NativePiUnavailable("Selected tool requires a trusted nominal mode")
        session.require_launch_tools(selected_tool_mode)
        cli = _trusted_package(package)
        worktree = Path(worktree).absolute()
        session_dir = Path(session_dir).absolute()
        _durable_private_session_dir(session_dir)
        if not worktree.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        session.require_launch_header()
        agent_dir = _private_agent_dir(session_dir)
        tool_arguments = (
            selected_tool_mode.launch_arguments(package)
            if selected_tool_mode is not None
            else ("--no-tools",)
        )
        arguments = [
            "--mode",
            "rpc",
            "--no-approve",
            *tool_arguments,
            "--session-dir",
            str(session_dir),
            "--provider",
            provider,
            "--model",
            model,
        ]
        arguments.extend(session.launch_arguments(thinking_level))
        if session_file is not None:
            arguments.extend(("--session", str(session_file)))
        env, configuration = session.launch_environment(agent_dir, environment)
        return cls._build(cli, tuple(arguments), worktree, env, session, package, configuration)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NativePiUnavailable("Ambiguous native Pi JSON object")
        result[key] = value
    return result


def _read_private_file(path: Path):
    """Yield strict private evidence rows, checking the same opened revision.

    Historical bytes are not an admission quota. A bounded byte snapshot is
    validated before decoding; callers must exhaust the resulting observation.
    """
    with PrivateEvidenceRead.open(path) as evidence:
        yield from evidence.rows()


class PrivateEvidenceRead:
    """Pinned private source with a verified original byte prefix.

    Resource custody lasts until close. Appending cannot invalidate earlier
    decoded bytes unnoticed: each read hashes the whole original prefix and
    checks both the descriptor and named revision across the observation.
    """

    def __init__(self, path, stream):
        self.path, self.stream = path, stream
        self.identity = FileIdentity.from_stat(os.fstat(stream.fileno()))
        self.size = 0
        self.digest = hashlib.sha256()

    @classmethod
    @contextmanager
    def open(cls, path):
        with ExitStack() as custody:
            try:
                stream = custody.enter_context(
                    os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb")
                )
                source = cls(path, stream)
            except OSError as error:
                raise NativePiUnavailable("Native Pi evidence file is unavailable") from error
            yield source

    def close(self):
        self.stream.close()

    def _capture_append(self):
        """Read original bytes under the file's own I/O exception boundary."""
        try:
            info = os.fstat(self.stream.fileno())
            PrivateFileRole.require(info)
            revision = FileRevision.from_stat(info)
            if revision.identity != self.identity:
                raise NativePiUnavailable("Native Pi evidence inode changed")
            if not info.st_size or info.st_size < self.size:
                raise NativePiUnavailable("Native Pi evidence file is empty or truncated")
            self.stream.seek(self.size)
            digest = self.digest.copy()
            appended = self.stream.read(info.st_size - self.size)
            if len(appended) != info.st_size - self.size or (appended and not appended.endswith(b"\n")):
                raise NativePiUnavailable("Native Pi evidence file is incomplete")
            digest.update(appended)
            for observed in (os.fstat(self.stream.fileno()), self.path.lstat()):
                PrivateFileRole.require(observed)
                if revision != FileRevision.from_stat(observed):
                    raise NativePiUnavailable("Native Pi evidence changed during observation")
            return info.st_size, digest, appended
        except (OSError, ValueError) as error:
            raise NativePiUnavailable("Native Pi evidence file is unavailable or not private") from error

    @staticmethod
    def _decode_row(raw):
        try:
            row = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique)
        except (UnicodeError, ValueError) as error:
            raise NativePiUnavailable("Native Pi evidence JSON is invalid") from error
        if type(row) is not dict:
            raise NativePiUnavailable("Native Pi evidence row has wrong type")
        return row

    def rows(self):
        try:
            size, digest, appended = self._capture_append()
            # Byte acquisition and decoding own their errors. Yielding these
            # original rows transfers no authority over a consumer's failures.
            for raw in appended.splitlines(keepends=True):
                yield self._decode_row(raw)
            try:
                self.verify_snapshot(size, digest.digest())
            except (OSError, ValueError) as error:
                raise NativePiUnavailable("Native Pi evidence file is unavailable or not private") from error
            self.size, self.digest = size, digest
        except BaseException:
            self.close()
            raise

    def verify_snapshot(self, size, expected_digest):
        """Validate the captured prefix after decoding, allowing later appends.

        The current revision must stay fixed during this short hash read. It
        need not equal the older capture revision: native appends during typed
        decoding are legitimate, while altered captured bytes are refused.
        """
        before = os.fstat(self.stream.fileno())
        PrivateFileRole.require(before)
        revision = FileRevision.from_stat(before)
        if revision.identity != self.identity or revision.size < size:
            raise NativePiUnavailable("Native Pi evidence snapshot source changed")
        self.stream.seek(0)
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            raw = self.stream.read(min(remaining, 128 * 1024))
            if not raw:
                raise NativePiUnavailable("Native Pi evidence prefix is incomplete")
            remaining -= len(raw)
            digest.update(raw)
        if digest.digest() != expected_digest:
            raise NativePiUnavailable("Native Pi evidence original prefix changed")
        for observed in (os.fstat(self.stream.fileno()), self.path.lstat()):
            PrivateFileRole.require(observed)
            if revision != FileRevision.from_stat(observed):
                raise NativePiUnavailable("Native Pi evidence changed during snapshot verification")


def _trusted_package(package: Path) -> Path:
    package = package.absolute()  # lexical: resolve() would hide a symlink component
    if ".." in package.parts:
        raise NativePiUnavailable("Pinned native Pi package path is not lexical")
    if package.parts[-3:] != (
        "node_modules",
        "@earendil-works",
        "pi-coding-agent",
    ):
        raise NativePiUnavailable("Canonical native Pi package layout is required")
    for ancestor in (package, *package.parents):
        info = ancestor.lstat()
        failed = TrustedAncestorRole.violation(info)
        if failed is not None:
            raise NativePiUnavailable(f"Native Pi package has unsafe ancestor: {failed.declared_name}")
    root = package.parents[2]
    if PrivateDirectoryRole.violation(root.lstat()) is not None:
        raise NativePiUnavailable("Disposable native Pi root must be owner-only")
    from .native_package import NativePackageError, verify_native_package

    try:
        verify_native_package(package)
    except (OSError, NativePackageError) as error:
        raise NativePiUnavailable("Pinned native Pi package differs from reviewed fork") from error
    return package / "dist/cli.js"


def _private_session_dir(directory: Path) -> None:
    if ".." in directory.parts:
        raise NativePiUnavailable("Native Pi session directory is not lexical")
    for ancestor in (directory, *directory.parents):
        info = ancestor.lstat()
        failed = TrustedAncestorRole.violation(info)
        if failed is not None:
            raise NativePiUnavailable(f"Native Pi session directory has unsafe ancestor: {failed.declared_name}")
    if PrivateDirectoryRole.violation(directory.lstat()) is not None:
        raise NativePiUnavailable("Native Pi session directory must be owner-only")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_private_session_dir(directory: Path) -> None:
    """Commit each new directory entry before a Pi launch can be returned.

    Re-sync the private ancestor chain even on reuse: a visible directory may
    have survived a failed parent fsync on the previous preparation. Stop at
    the parent of the highest owner-only directory, not at the filesystem root.
    """
    missing: list[Path] = []
    current = directory
    while True:
        try:
            current.lstat()
        except FileNotFoundError:
            missing.append(current)
            current = current.parent
        else:
            break
    try:
        for path in reversed(missing):
            path.mkdir(mode=0o700, exist_ok=True)
            _private_session_dir(path)
            _fsync_directory(path.parent)
        _private_session_dir(directory)
        parent = directory.parent
        while True:
            _fsync_directory(parent)
            info = parent.lstat()
            if parent == parent.parent or PrivateDirectoryRole.violation(info) is not None:
                break
            parent = parent.parent
    except OSError as error:
        raise NativePiUnavailable("Native Pi session directory could not be committed") from error


def _private_agent_dir(session_dir: Path) -> Path:
    """Durably isolate Pi settings from user/global and project retry policy.

    Refresh and fsync the exact policy for *every* attempt; a previously visible
    settings file or directory is not evidence that an earlier fsync succeeded.
    No credentials are copied into this directory (provider auth uses the env).
    """
    agent_dir = session_dir / ".native-pi-agent"
    try:
        agent_dir.mkdir(mode=0o700, exist_ok=True)
        _private_session_dir(agent_dir)
        _fsync_directory(session_dir)
        temporary = agent_dir / f".settings-{uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(_NATIVE_SETTINGS)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, agent_dir / "settings.json")
            _fsync_directory(agent_dir)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise NativePiUnavailable(
            "Native Pi private retry policy could not be committed"
        ) from error
    return agent_dir


def _session_location(directory: Path, candidate: str) -> Path:
    if not isinstance(candidate, str) or not candidate:
        raise NativePiUnavailable("Native Pi omitted its session file")
    path = Path(candidate)
    if not path.is_absolute() or path.parent != directory or path.suffix != ".jsonl":
        raise NativePiUnavailable("Native Pi changed its private session location")
    return path


def read_tracked_input_digest(
    session_file: Path, input_id: str, *, evidence: NativeEvidenceRead | None = None
) -> str:
    """Corroborating digest only; this cannot authorize recovery or input replay."""
    NativeInputIdText.decode(input_id)
    session_file = Path(session_file).absolute()
    from .native_entries import NativeInputEvidenceRead

    with NativeInputEvidenceRead.borrow(session_file, evidence) as evidence:
        _header, entries = evidence.observe()
        users = NativeEntry.tracked_users(entries)
        if input_id not in users:
            raise NativePiUnavailable("The specified input was never durably committed")
        return users[input_id].message.input_digest


def _verify_context(
    session_file: Path,
    input_id: str,
    session_id: str,
    input_event: pi.InputCommitted,
    context_event: pi.ContextCommitted,
    *, evidence: NativeEvidenceRead | None = None,
) -> NativeContextProof:
    try:
        emitted = NativeContextRecord.from_events(input_id, session_id, input_event, context_event).at(session_file)
    except (TypeError, ValueError) as error:
        raise NativePiUnavailable("Native Pi input/context receipt is malformed") from error
    with NativeEvidenceRead.borrow(session_file, evidence) as evidence:
        proof = NativeContextProof.read_evidence(session_file, input_id, evidence=evidence)
        if proof != emitted:
            raise NativePiUnavailable("Native Pi emitted an event without matching durable proof")
        return proof


def _require_reviewed_selected_source_cli() -> None:
    """Default OFF until copied CLI startup factories are independently fenced."""
    raise NativePiUnavailable(
        "Selected first-source CLI builtins are unreviewed; no Pi startup or raw prompt"
    )


def _fresh_selected_revision(
    fresh: FreshPrivateSession, *, started: bool = False
) -> FileRevision:
    """Exact saved inode+revision; not a provider or terminal receipt."""
    if started:
        return fresh.verify_selected_startup()
    fresh.verify_prewrite()
    info = fresh.path.lstat()
    return FileRevision.from_stat(info)
