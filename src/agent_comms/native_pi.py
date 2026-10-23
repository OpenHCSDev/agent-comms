"""Pinned Pi RPC execution with durable attempt-bound context evidence.

Native proof records the model context; the coordinator owns disposition and
publication. Verified package and send authority remain required for every turn.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import JsonValue

from agent_comms.participant_identity import ParticipantIdentity

from .locked_store import SQLiteFile
from .errors import RelationViolationError
from .field_codec import (
    FieldCodec,
    Id128Text,
    MalformedRecord,
    Sha256Text,
    WireName,
    boundary_decoder,
)
from .launch_root import LaunchRoot
from .launch_runtime import LaunchRuntime, RestartEnvironment, ThreadLaunch
from .native_arguments import NativeArguments, SelectedModel
from .native_failures import NativePiUnavailable, NativeRequestFailure
from .native_input_record import NativeInputCommit, NativeInputLocation
from .native_package import MANIFEST, UNIT_INSTRUCTIONS, VerifiedNativePackage
from .native_session_files import NativeAgentSettings, NativeSessionFiles
from .native_tool_call import NativeToolMode
from .pi_vocabulary import NoThinkingOverride, PendingStopReason, ThinkingChoice
from .private_path import (
    FileIdentity,
    FileRevision,
    PrivateFileRole,
    TrustedAncestorRole,
)
from .typed_table import (
    Column,
    DurableTables,
    GuardedInserts,
    ImmutableRows,
    Index,
    WithoutRowid,
)

if TYPE_CHECKING:
    from . import pi_events as pi
    from .native_entries import NativeEntry, NativeEvidenceRead, SessionEntry
    from .selected_session import SelectedSession
    from .turn_failure import TurnFailure

CAPABILITY = "pi-native-input-v1-live-only"


class NativeEmptyAnswer(NativeRequestFailure):  # noqa: N818 - nominal model outcome
    """The model completed, but supplied no answer text; the runtime is available."""

    def __init__(self) -> None:
        super().__init__("The model finished with no text")


class NativeInputUncommitted(NativePiUnavailable):
    """The observed session holds no durable commit of the asked input; its turn is pending."""

    @property
    def stop_reason(self) -> type[PendingStopReason]:
        return PendingStopReason


class InputCommitment:
    """What the observed session durably committed for one input, asked for a prompt digest."""

    def matches(self, digest: str) -> bool:
        return False


@dataclass(frozen=True)
class CommittedInput(InputCommitment):
    digest: str

    def matches(self, digest: str) -> bool:
        return self.digest == digest


class UncommittedInput(InputCommitment):
    """No entry commits the input, so no expected digest can equal it."""


class NativePiInputNotSent(NativePiUnavailable):
    """The input never entered its prompt writer; this does not permit replay."""

    def __init__(self, error, attestation):
        self.attestation = attestation
        super().__init__(f"Native Pi failed before prompt claim_scope; input not sent: {error}")

    def diagnostic_evidence(self):
        return {
            **super().diagnostic_evidence(),
            "input_disposition": "not_sent",
            "initialization": self.attestation.diagnostic_evidence,
        }

    @property
    def public_failure(self):
        return str(self)


class NativePiPromptRejected(NativePiUnavailable):
    """A refusal matched to its RPC request; it permits neither retry nor new input."""

    def __init__(self, response: pi.Response):
        self.response = response
        super().__init__("Native Pi rejected the tracked prompt (see diagnostic)")

    def publish(self, publisher):
        return publisher(self.public_failure, native_response=self.response, source_error=self)


class NativePiOperationFailed(NativePiUnavailable):
    """Keep the native event private while reporting a fixed public failure."""

    def __init__(self, event: pi.NativeOperationError) -> None:
        self.event = event
        super().__init__("Native Pi refused an operation (see diagnostic)")

    def diagnostic_evidence(self) -> dict[str, object]:
        return {**super().diagnostic_evidence(), "operation": self.event.error}


def main() -> int:
    """Run the active route's pinned Pi for ordinary ACP owner sessions."""
    from .active_route import ActiveRoute, resolve_comms_route
    LaunchRoot.startup(sys.argv, ActiveRoute.published_launch)
    package = resolve_comms_route().launch().require_private().validate()
    environment = dict(os.environ)
    argv, environment = NativePiRpcLaunch.bootstrap(
        package, tuple(sys.argv[1:]), Path.cwd(), environment,
        RestartEnvironment.inherit(environment),
        LaunchRuntime.running(),
    )
    os.execvpe(argv[0], list(argv), environment)
    return 0


@dataclass(frozen=True, slots=True)
class NativeContextRecord(NativeInputCommit):
    """Native context facts extend the original committed input declaration."""

    request_generation: Annotated[
        int,
        WireName("requestGeneration"),
        Column(primary_key=True, check="request_generation BETWEEN 1 AND 9007199254740991"),
    ]
    llm_context_digest: Annotated[
        str,
        WireName("llmContextDigest"),
        Column(check="length(llm_context_digest)=64 AND llm_context_digest NOT GLOB '*[^a-f0-9]*'"),
    ]
    # The native writer stamps the moment this context was committed for a
    # model request: the time the input was shown to the model.
    committed_at_ms: Annotated[
        int,
        WireName("committedAtMs"),
        Column(check="committed_at_ms BETWEEN 1 AND 9007199254740991"),
    ]

    @classmethod
    def from_events(cls, input_id, session_id, input_event, context_event):
        """Strictly join the two emitted records through the context declaration.

        This is an emitted acknowledgement, not proof of durable inclusion. A journal
        read must independently corroborate the same complete located record.
        """
        emitted = FieldCodec.encode(context_event)
        record = FieldCodec.decode(cls, {key: emitted[key]
                                         for _, key in FieldCodec.record_fields(cls)})
        if record.input_commit != NativeInputCommit(input_id, session_id, record.session_entry_id):
            raise NativePiUnavailable("Native Pi context names another original input")
        if record.input_commit != input_event.input_commit:
            raise NativePiUnavailable("Native Pi input/context events disagree")
        return record

    def __post_init__(self):
        Sha256Text.decode(self.llm_context_digest)
        if not 0 < self.request_generation <= 2**53 - 1:
            raise ValueError("Native context generation is outside its native range")
        if not 0 < self.committed_at_ms <= 2**53 - 1:
            raise ValueError("Native context commit time is outside its native range")

    def at(self, session_file: Path, *, session_entry_start: int) -> NativeContextProof:
        """Locate recorded facts; this does not grant acceptance or replay."""
        return NativeContextProof(
            self.input_id,
            self.session_id,
            self.session_entry_id,
            self.request_generation,
            self.llm_context_digest,
            self.committed_at_ms,
            session_file,
            session_entry_start=session_entry_start,
        )


@dataclass(frozen=True, slots=True)
class NativeContextJournal(
    GuardedInserts, ImmutableRows, NativeContextRecord, WithoutRowid, DurableTables
):
    """One proof declaration owns wire facts and the indexed durable journal."""

    insert_condition = """NEW.request_generation < COALESCE((SELECT MAX(request_generation)
        FROM native_context_journal), 0) OR EXISTS(SELECT 1 FROM native_context_journal
        WHERE request_generation=NEW.request_generation
        AND (llm_context_digest != NEW.llm_context_digest
        OR committed_at_ms != NEW.committed_at_ms)) OR NEW.session_id
        != (SELECT session_id FROM native_context_journal ORDER BY session_id LIMIT 1)"""


    schema: Literal[2]
    type: Literal["context_committed"]

    indexes = (Index(("request_generation",)), Index(("session_id",)))

    @classmethod
    def native_contract(cls) -> dict[str, JsonValue]:
        """Generate the native writer's SQLite contract from this declaration."""
        table = cls.declared_name
        return {
            "objects": cls.schema_objects(),
            "columns": [
                {"name": plan.name, "wire": plan.key} for plan in FieldCodec.field_plans(cls)
            ],
            "insert": cls.insertion_statement(),
            "head": f"SELECT COALESCE(MAX(request_generation),0) AS generation FROM {table}",
            "session": f"SELECT session_id AS id FROM {table} ORDER BY session_id LIMIT 1",
            "current": f"SELECT input_id,session_entry_id FROM {table} "
            f"WHERE request_generation=(SELECT MAX(request_generation) FROM {table})",
        }

    @classmethod
    @contextmanager
    def open_evidence(cls, files: NativeSessionFiles) -> Iterator[sqlite3.Connection]:
        """Read an indexed snapshot after SQLite's bounded hot-journal recovery.

        A read is corroboration, never a fresh acknowledgement. Keep the private inode
        pinned across SQLite's transaction and refuse redirection or schema drift.
        Open existing storage read/write for SQLite rollback, then forbid SQL
        writes. A cold history view can recover without a model turn or acknowledgement.
        """
        path = files.input_proof
        with ExitStack() as ownership:
            try:
                held = ownership.enter_context(closing(
                    os.fdopen(PrivateFileRole.read(
                        path, NativePiUnavailable("Native proof indexed evidence is unavailable")), "rb")
                ))
                before = os.fstat(held.fileno())
                db = ownership.enter_context(closing(SQLiteFile.recovering_snapshot(path)))
                if cls.schema_differs(db):
                    raise NativePiUnavailable("Native proof requires offline durable conversion")
                if FileIdentity.from_stat(before) != FileIdentity.from_stat(path.lstat()):
                    raise NativePiUnavailable("Native proof inode changed while opening")
            except (OSError, sqlite3.Error, RelationViolationError, MalformedRecord) as error:
                raise NativePiUnavailable("Native proof indexed evidence is unavailable") from error
            # Consumer exceptions retain their original boundary and disposition.
            # The ExitStack closes the database and descriptor without handling caller errors.
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
                f"SELECT {cls._column_list(tuple(cls.column_projection()))} FROM {cls.declared_name} "
                f"WHERE {where} ORDER BY request_generation DESC LIMIT 1",
                parameters,
            )
        )
        return next(iter(rows), None)

    def corroborate(
        self, session_file: Path, header: SessionEntry, entry: NativeEntry, *,
        session_entry_start: int,
    ) -> NativeContextProof:
        if self.session_id != header.id or self.session_entry_id != entry.id:
            raise NativePiUnavailable("Native Pi proof journal source lineage differs")
        return self.at(session_file, session_entry_start=session_entry_start)


@dataclass(frozen=True, slots=True)
class NativeContextProof(NativeContextRecord, NativeInputLocation):
    session_file: Path

    def corroborates_input(
        self, input_id: str, *, files: NativeSessionFiles,
        evidence: NativeEvidenceRead,
    ) -> bool:
        """Check an already-observed live event against its isolated saved proof.

        This grants neither replay nor recovery authority. History IO occurs
        before the coordinator read transaction, as on the native return path.
        """
        if self.input_id != input_id or files.session != self.session_file:
            return False
        return self.read_evidence(
            files, input_id,
            request_generation=self.request_generation, evidence=evidence,
            session_entry_start=self.session_entry_start,
        ) == self

    @classmethod
    @boundary_decoder
    def read_evidence(
        cls, files: NativeSessionFiles, input_id: str, *, request_generation: int | None = None,
        evidence: NativeEvidenceRead | None = None,
        session_entry_start: int,
    ) -> NativeContextProof:
        """Corroborate live recorded events; parsed bytes alone grant no authority."""
        Id128Text.decode(input_id)
        session_file = files.session.absolute()
        from .native_entries import NativeInputEvidenceRead
        with NativeInputEvidenceRead.borrow(session_file, evidence) as evidence:
            with NativeContextJournal.open_evidence(files) as db:
                row = NativeContextJournal.for_input(db, input_id, request_generation)
            if row is None:
                raise NativePiUnavailable("The input has no assembled-context proof")
            location = NativeInputLocation(input_id, row.session_id, row.session_entry_id,
                                           session_entry_start=session_entry_start)
            entry = evidence.read_input(location)
            return row.corroborate(session_file, evidence.header(), entry,
                                   session_entry_start=session_entry_start)


class NativePiTerminalFailure(NativePiUnavailable):
    """A proved input ended in a failed terminal and its owned process was reaped."""

    def __init__(
        self, detail: str, context: NativeContextProof, model: SelectedModel,
        failure: TurnFailure,
    ):
        super().__init__(f"Native Pi assistant did not finish successfully: {detail}")
        self.context = context
        self.model = model
        self.failure = failure
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
            f"{model.qualified}: {match.group(1) if match else 'request failed'}."
        )


@dataclass(frozen=True, slots=True)
class NativeTurnResult:
    def require_publishable(self) -> None:
        from .coordination_errors import IdentityConflict

        if not self.text:
            raise IdentityConflict("successful model produced no publishable response")

    text: str
    context: NativeContextProof


@dataclass(frozen=True, slots=True)
class NativePiRpcLaunch:
    """Verified native Pi subprocess launch, not an authorization to send input."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    session: SelectedSession
    package: VerifiedNativePackage

    root: Path = field(kw_only=True)
    configuration: RestartEnvironment = field(kw_only=True)
    runtime: LaunchRuntime = field(kw_only=True)

    @classmethod
    def bootstrap(
        cls, package: VerifiedNativePackage, arguments, cwd, environment, configuration: RestartEnvironment,
        runtime: LaunchRuntime,
    ):
        """Encode the held launch configuration at the native process boundary."""
        try:
            instructions = UNIT_INSTRUCTIONS.resolve(strict=True)
        except OSError as error:
            raise NativePiUnavailable("Unit instructions were not prepared with the native package") from error
        env = package.node_environment(environment)
        env[ThreadLaunch.variable("worktree")] = str(cwd)
        env = runtime.environment(env)
        env.update(configuration.encode_native())
        argv = (
            *package.node("agent-comms-project-bootstrap.mjs"),
            *arguments, "--append-system-prompt", str(instructions),
        )
        return argv, env

    @classmethod
    def _build(cls, package, arguments, cwd, environment, session, configuration, runtime, root):
        argv, env = cls.bootstrap(package, arguments, cwd, environment, configuration, runtime)
        return cls(argv, cwd, env, session, package, root=root, configuration=configuration, runtime=runtime)

    @classmethod
    def _command_selection(
        cls, command: str, runtime: LaunchRuntime, package: VerifiedNativePackage,
    ):
        """Decode the original launcher selection without acquiring its package.

        Executable names never establish capability. Configured commands must
        name this installation's entrypoint, its pinned CLI, or the source stack
        launcher; execution always uses the verified native package.
        """
        stack_launcher = MANIFEST.parent / "bin" / "pi-native"
        executable = (
            Path(shutil.which(command) or command).resolve(strict=True) if command != "pi" else None
        )
        allowed = (
            Path(NativeArguments.owner_launcher(runtime)).resolve(),
            stack_launcher.resolve(),
            package.cli.resolve(),
        )
        if executable is not None and executable not in allowed:
            raise NativePiUnavailable("Configured command is not a validated native Pi launcher")

    @classmethod
    def package_for_command(
        cls, command: str, runtime: LaunchRuntime, root: Path | str | None = None,
    ) -> VerifiedNativePackage:
        """Acquire the original reviewed artifact before any new native execution.

        The launch is the caller's root's; without one, this process's selection.
        """
        from .active_route import resolve_comms_route

        package = resolve_comms_route(root).launch().launched_package()
        cls._command_selection(command, runtime, package)
        return package

    @classmethod
    def catalog(
        cls, command: str, arguments: tuple[str, ...], *, package: VerifiedNativePackage, worktree: Path,
        root: Path | str | None = None,
    ) -> NativePiRpcLaunch:
        """Metadata-only RPC owns an in-memory session, never a saved selection.

        Validate the configured arguments as managed RPC first. Only this
        launch owner supplies the native no-persistence choice; callers cannot
        replace a managed participant's selected source through CLI options.
        """
        from .selected_session import SelectedSession

        try:
            arguments = NativeArguments.parse(arguments).rpc()
        except ValueError as error:
            raise NativePiUnavailable(str(error)) from error
        runtime = LaunchRuntime.running()
        cls._command_selection(command, runtime, package)
        cwd = worktree.resolve(strict=True)
        if not cwd.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        return cls._managed(package, (*arguments, "--no-extensions", "--no-skills",
            "--no-context-files", "--no-session"), cwd, None, SelectedSession(cwd), runtime, cwd)

    @classmethod
    def managed(
        cls,
        command: str,
        arguments: tuple[str, ...],
        *,
        package: VerifiedNativePackage,
        worktree: Path,
        root: Path,
        owner: ParticipantIdentity,
        directory: Path,
        environment: dict[str, str] | None = None,
        session_file: str | None = None,
    ) -> NativePiRpcLaunch:
        """Prepare managed ACP/headless execution; native evidences remain separate."""
        try:
            arguments = NativeArguments.parse(arguments).managed_rpc(package.path)
        except ValueError as error:
            raise NativePiUnavailable(str(error)) from error
        runtime = LaunchRuntime.running()
        cls._command_selection(command, runtime, package)
        cwd = worktree.resolve(strict=True)
        if not cwd.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        from .selected_session import SelectedSession

        saved = Path(session_file).absolute() if session_file is not None else None
        NativeSessionFiles.prepare_private_directory(directory)
        session = SelectedSession.for_launch(directory, saved, root=root, worktree=cwd, owner=owner)
        return cls._managed(package, arguments, cwd, environment, session, runtime, root)

    @classmethod
    def _managed(cls, package, arguments, cwd, environment, session, runtime, root):
        """Assemble the entire key from one acquired package and selected source."""
        cwd = Path(cwd).resolve(strict=True)
        if not cwd.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        env = dict(os.environ)
        env.update(environment or {})
        arguments += ("--session-dir", str(session.directory))
        arguments += session.launch_arguments(NoThinkingOverride)
        arguments += session.proof_arguments(root)
        if session.path is not None:
            arguments += ("--session", str(session.path))
        return cls._build(package, arguments, cwd, env, session, RestartEnvironment.inherit(env), runtime, root)

    def retained_managed(
        self, command: str, arguments: tuple[str, ...], *, worktree: Path, root: Path,
        directory: Path, owner: ParticipantIdentity,
        environment: dict[str, str] | None, session_file: str | None,
    ) -> NativePiRpcLaunch | None:
        """Derive a candidate from this acquired immutable launch, not a path cache.

        Only the session that owns the child may reuse it after comparing the complete
        launch/auth key and checking its actual saved-source revision/liveness.
        A different package or selected file needs its own fresh acquisition.
        """
        self._command_selection(command, self.runtime, self.package)
        saved = Path(session_file).absolute() if session_file is not None else None
        if ((saved, directory) != (self.session.path, self.session.directory)
                or self.session.files(root).owner.lookup != owner.lookup):
            return None
        return self._managed(self.package,
            NativeArguments.parse(arguments).managed_rpc(self.package.path),
            worktree, environment, self.session, self.runtime, root)

    @classmethod
    def tracked(cls, package: VerifiedNativePackage, **options) -> NativePiRpcLaunch:
        """Construct a new source/auth launch from the process's acquired artifact."""
        return cls._tracked(package, LaunchRuntime.running(), **options)

    @classmethod
    def _tracked(
        cls,
        package: VerifiedNativePackage,
        runtime: LaunchRuntime,
        *,
        worktree: Path,
        root: Path,
        session: SelectedSession,
        model: SelectedModel,
        thinking_level: type[ThinkingChoice] = NoThinkingOverride,
        environment: dict[str, str] | None = None,
        selected_tool_mode: NativeToolMode,
    ) -> NativePiRpcLaunch:
        """Construct a complete launch through its acquired immutable artifact.

        A backend must explicitly consume this launch, not guess Pi from a basename
        or trust an RPC capability response from an arbitrary executable. This
        only establishes the executable and its resources; native input, context,
        and model-delivery proofs remain separate per-attempt observations.
        """
        session_dir, session_file = session.directory, session.path
        worktree = Path(worktree).absolute()
        session_dir = Path(session_dir).absolute()
        NativeSessionFiles.prepare_private_directory(session_dir)
        if not worktree.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        # Native SettingsManager and AgentSession own tracked retry/compaction
        # policy. This directory isolates writable resources, not policy copies.
        agent_dir = NativeAgentSettings.prepare_directory(session_dir)
        tool_arguments = selected_tool_mode.launch_arguments(package.path)
        arguments = [
            "--mode",
            "rpc",
            "--no-approve",
            *tool_arguments,
            "--session-dir",
            str(session_dir),
            *model.argv,
        ]
        arguments.extend(session.launch_arguments(thinking_level))
        arguments.extend(session.proof_arguments(root))
        if session_file is not None:
            arguments.extend(("--session", str(session_file)))
        env, configuration = session.launch_environment(agent_dir, environment)
        return cls._build(package, tuple(arguments), worktree, env, session, configuration, runtime, root)


def _read_private_file(path: Path):
    """Yield strict private evidence rows, checking the same opened revision.

    Historical bytes do not set the live input budget. A bounded byte snapshot is
    validated before decoding; callers must exhaust the resulting observation.
    """
    with PrivateEvidenceRead.open(path) as evidence:
        yield from evidence.rows()


class NativeEvidencePrefixChanged(NativePiUnavailable):
    """The retained verified prefix must be reacquired from byte zero."""


class NativeEvidenceRemoved(NativePiUnavailable):
    """The recorded recipient session no longer exists at its owned path."""


class UnverifiedNativeEvidencePrefix:
    """No captured cut yet; the private writer owns append-only content."""

    size = 0

    @staticmethod
    def prepare(source: PrivateEvidenceRead, revision: FileRevision) -> FileRevision:
        return revision

    @staticmethod
    def commit(source: PrivateEvidenceRead, size: int, revision: FileRevision) -> FileRevision:
        return source.verify_snapshot(size, revision)


@dataclass(frozen=True, slots=True)
class VerifiedNativeEvidencePrefix(UnverifiedNativeEvidencePrefix):
    """The retained cut and revision of one acquired private inode."""

    size: int = field()
    revision: FileRevision

    def prepare(self, source: PrivateEvidenceRead, revision: FileRevision) -> FileRevision:
        return source.verify_snapshot(self.size, self.revision)


class PrivateEvidenceRead:
    """Pinned private source with one owned verified-prefix state.

    This reader holds its descriptor until close. The private writer owns its
    append-only bytes. Reads check the inode, captured size and complete final
    record, then decode only beyond their consumed position. No history is hashed.
    """

    def __init__(self, path, stream):
        self.path, self.stream = path, stream
        self.identity = FileIdentity.from_stat(os.fstat(stream.fileno()))
        self.prefix = UnverifiedNativeEvidencePrefix()
        self.decoded_through = stream.tell()

    @property
    def size(self) -> int:
        return self.prefix.size

    @classmethod
    def _open_stream(cls, path):
        stream = None
        try:
            if path != path.resolve(strict=True):
                raise NativePiUnavailable("Native Pi evidence session path is not canonical")
            stream = os.fdopen(PrivateFileRole.read(
                path, NativePiUnavailable("Native Pi evidence file is unavailable")), "rb")
            return stream
        except FileNotFoundError as error:
            if stream is not None:
                stream.close()
            raise NativeEvidenceRemoved("Native Pi evidence session was removed") from error
        except (OSError, ValueError) as error:
            if stream is not None:
                stream.close()
            raise NativePiUnavailable("Native Pi evidence file is unavailable") from error

    @classmethod
    @contextmanager
    def open(cls, path):
        source = cls(path, cls._open_stream(path))
        try:
            yield source
        finally:
            source.close()

    def close(self):
        self.stream.close()

    def reverify(self) -> None:
        """Reopen the source; its reader restores the required input position."""
        self.close()
        self.stream = self._open_stream(self.path)
        self.identity = FileIdentity.from_stat(os.fstat(self.stream.fileno()))
        self.prefix = UnverifiedNativeEvidencePrefix()
        self.decoded_through = self.stream.tell()

    def _capture_append(self):
        """Capture a complete private cut without reading its prior bytes."""
        try:
            info = os.fstat(self.stream.fileno())
            revision = self._observe_revision_after(FileRevision.from_stat(info))
            if not info.st_size:
                raise NativePiUnavailable("Native Pi evidence file is empty")
            if info.st_size < self.size:
                raise NativeEvidencePrefixChanged(
                    "Native Pi evidence verified prefix was truncated"
                )
            revision = self.prefix.prepare(self, revision)
            if info.st_size > self.size:
                last = os.pread(self.stream.fileno(), 1, info.st_size - 1)
                if last != b"\n":
                    raise NativePiUnavailable("Native Pi evidence file is incomplete")
            observed = self._observe_revision_after(revision)
            if not revision.holds_cut(observed, info.st_size):
                raise NativeEvidencePrefixChanged(
                    "Native Pi evidence source changed during observation"
                )
            return info.st_size, revision
        except (OSError, ValueError) as error:
            raise NativePiUnavailable(
                "Native Pi evidence file is unavailable or not private"
            ) from error

    def _decode_captured(self, size):
        """Decode through a fresh buffer over the pinned descriptor each time."""
        try:
            descriptor = os.dup(self.stream.fileno())
        except (OSError, ValueError) as error:
            raise NativePiUnavailable(
                "Native Pi evidence file is unavailable or not private"
            ) from error
        try:
            decoder = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            try:
                decoder.seek(self.decoded_through)
            except (OSError, ValueError) as error:
                raise NativePiUnavailable(
                    "Native Pi evidence file is unavailable or not private"
                ) from error
            remaining = size - self.decoded_through
            while remaining:
                try:
                    raw = decoder.readline(remaining)
                except (OSError, ValueError) as error:
                    raise NativePiUnavailable(
                        "Native Pi evidence file is unavailable or not private"
                    ) from error
                if not raw or not raw.endswith(b"\n"):
                    raise NativePiUnavailable("Native Pi evidence file is incomplete")
                remaining -= len(raw)
                yield self._decode_row(raw)
        finally:
            decoder.close()

    @staticmethod
    def _decode_row(raw):
        try:
            row = FieldCodec.parse(raw)
        except MalformedRecord as error:
            raise NativePiUnavailable("Native Pi evidence JSON is invalid") from error
        if type(row) is not dict:
            raise NativePiUnavailable("Native Pi evidence row has wrong type")
        return row

    def header(self):
        """Return row one after verifying the complete captured byte prefix."""
        try:
            size, revision = self._capture_append()
            self.stream.seek(0)
            raw = self.stream.readline(size)
            if not raw or not raw.endswith(b"\n"):
                raise NativePiUnavailable("Native Pi evidence header is incomplete")
            header = self._decode_row(raw)
            self._commit_snapshot(size, revision)
            return header
        except BaseException:
            self.close()
            raise

    def consume_header(self):
        """Consume only record one; verification and decode positions are distinct."""
        header = self.header()
        self.decoded_through = max(self.decoded_through, self.stream.tell())
        return header

    def verify(self) -> None:
        """Validate this acquired source without decoding or moving its input position."""
        size, revision = self._capture_append()
        self._commit_snapshot(size, revision)

    def start_at(self, offset: int) -> None:
        """Seek to a recorded boundary after checking the original source revision."""
        FieldCodec.decode(int, offset)
        size, revision = self._capture_append()
        if not 0 < offset < size or os.pread(self.stream.fileno(), 1, offset - 1) != b'\n':
            raise NativePiUnavailable('Native input byte offset is not a complete source boundary')
        self._commit_snapshot(size, revision)
        self.decoded_through = offset

    def _commit_snapshot(self, size, revision) -> None:
        try:
            observed = self.prefix.commit(self, size, revision)
        except (OSError, ValueError) as error:
            raise NativePiUnavailable(
                "Native Pi evidence file is unavailable or not private"
            ) from error
        self.prefix = VerifiedNativeEvidencePrefix(size, observed)

    def rows(self):
        try:
            previous_size = self.decoded_through
            size, revision = self._capture_append()
            if size == previous_size:
                self.prefix = VerifiedNativeEvidencePrefix(size, revision)
                return
            yield from self._decode_captured(size)
            self._commit_snapshot(size, revision)
            self.decoded_through = size
        except BaseException:
            self.close()
            raise

    def verify_snapshot(self, size, revision):
        """Keep the acquired inode and captured cut; trust our append-only writer."""
        observed = self._observe_revision_after(revision)
        if observed.identity != self.identity or observed.size < size:
            raise NativeEvidencePrefixChanged("Native Pi evidence snapshot source changed")
        return observed

    def _observe_revision_after(self, revision):
        """Check original private inode and monotonic growth, not byte integrity.

        Each observation fences the next: growth seen on the descriptor cannot
        be hidden by shrinking the named file back.
        """
        try:
            TrustedAncestorRole.require_path(
                self.path.parent, NativePiUnavailable("Native Pi evidence has an unsafe ancestor"))
        except OSError as error:
            raise NativePiUnavailable("Native Pi evidence has an unsafe ancestor") from error
        # Detect replacement before classifying the old descriptor's now-zero
        # links: observing a new named source takes the existing reverify path.
        for info in (self.path.lstat(), os.fstat(self.stream.fileno())):
            PrivateFileRole.require(info, RelationViolationError("Native Pi evidence file is not private"))
            observed = FileRevision.from_stat(info)
            if observed.identity != self.identity or (
                observed != revision and observed.size <= revision.size
            ):
                raise NativeEvidencePrefixChanged(
                    "Native Pi evidence source changed without growth"
                )
            revision = observed
        return revision


def _session_location(directory: Path, candidate: str) -> Path:
    if not isinstance(candidate, str) or not candidate:
        raise NativePiUnavailable("Native Pi omitted its session file")
    path = Path(candidate)
    if not path.is_absolute() or path.parent != directory or path.suffix != ".jsonl":
        raise NativePiUnavailable("Native Pi changed its private session location")
    return path


def read_tracked_input_commitment(
    session_file: Path, input_id: str, *, evidence: NativeEvidenceRead | None = None
) -> InputCommitment:
    """Corroborating commitment only; this cannot authorize recovery or input replay.

    An unreadable or malformed journal is not an uncommitted input and raises.
    """
    Id128Text.decode(input_id)
    session_file = Path(session_file).absolute()
    from .native_entries import NativeEvidenceRead

    with NativeEvidenceRead.borrow(session_file, evidence) as evidence:
        evidence.observe()
        try:
            return CommittedInput(evidence.tracked(input_id).message.input_digest.require())
        except NativeInputUncommitted:
            return UncommittedInput()


def _verify_context(
    files: NativeSessionFiles,
    input_id: str,
    session_id: str,
    input_event: pi.InputCommitted,
    context_event: pi.ContextCommitted,
    *, evidence: NativeEvidenceRead | None = None,
) -> NativeContextProof:
    from .native_entries import NativeInputEvidenceRead

    session_file = files.session
    try:
        emitted = NativeContextRecord.from_events(input_id, session_id, input_event, context_event).at(
            session_file, session_entry_start=input_event.session_entry_start)
    except MalformedRecord as error:
        raise NativePiUnavailable("Native Pi input/context acknowledgement is malformed") from error
    with NativeInputEvidenceRead.borrow(session_file, evidence) as evidence:
        if not emitted.corroborates_input(input_id, files=files, evidence=evidence):
            raise NativePiUnavailable("Native Pi emitted an event without matching durable proof")
        return emitted
