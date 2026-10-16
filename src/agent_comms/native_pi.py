"""Pinned Pi RPC execution with durable attempt-bound context evidence.

Native proof records the model context; the coordinator owns disposition and
publication. Verified package and send authority remain required for every turn.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field, fields
from functools import partial
from pathlib import Path
from typing import Any, Literal

from . import pi_events as pi
from .context_segments.doctrine import DoctrineSegment
from .coordination_database import CoordinationStore
from .coordination_errors import IdentityConflict
from .launch_runtime import LaunchRuntime, RestartEnvironment
from .field_codec import FieldCodec
from .fresh_private_session import FreshPrivateSession
from .native_arguments import (
    ExtensionArgument, ExternalArgument, NativeArguments, NoSessionArgument,
    NoToolsArgument, SessionArgument, SessionDirArgument,
)
from .native_entries import NativeEntry, NativeEvidenceRead, NativeInputEvidenceRead, SessionEntry
from .native_errors import NativePiUnavailable
from .native_input_record import NativeInputCommit, NativeInputIdText
from .native_package import MANIFEST, OWNER_INSTRUCTIONS, NativePackageError, verify_native_package
from .native_session_files import NativeSessionFiles
from .native_session_reopen import SessionIdentityHelper
from .private_nk_entrypoint import PrivateNkLaunch
from .private_path import (
    FileIdentity,
    FileRevision,
    PrivateDirectoryRole,
    PrivateFileRole,
    TrustedAncestorRole,
)
from .registry_store import RegistryStore
from .native_project import NativeProject, ManagedNativeProject, UnmanagedNativeProject
from .native_tools import NativeRequestTools
from .selected_session import SavedSelectedSession, SelectedSession
from .selected_tool_broker import NativeToolMode
from .thread_identity import ThreadIncarnation
from .turn_context import ContextSegment, TurnContext
from .turn_failure import TurnFailure
from .typed_table import Column, Index, SQLiteSchemaObject, TypedTable

CAPABILITY = "pi-native-input-v1-live-only"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


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
    launch = PrivateNkLaunch.current()
    if launch is None:
        raise NativePiUnavailable("Native owner backend requires a configured private route")
    cli = launch.validate()
    parser = argparse.ArgumentParser(add_help=False)
    ThreadIncarnation.add_cli_arguments(parser, required=True, dest="thread_incarnation")
    selection, arguments = parser.parse_known_args(sys.argv[1:])
    incarnation = selection.thread_incarnation.require_recorded()
    incarnation.require_recorded()
    entry = RegistryStore(launch.validated_root / "registry.json").read_entry(incarnation.name).entry
    owner = entry.thread
    if owner.created_at != incarnation.created_at:
        raise NativePiUnavailable("Native launch belongs to a different thread incarnation")
    session_context = TurnContext.session_segments(owner)
    environment = dict(os.environ)
    project = ManagedNativeProject.from_entry(launch.validated_root, entry)
    argv, environment = NativePiRpcLaunch.bootstrap(
        cli,
        NativeArguments.parse(arguments),
        Path.cwd(),
        environment,
        RestartEnvironment.inherit(environment),
        LaunchRuntime.running(),
        session_context=session_context, project=project,
    )
    os.execvpe(argv[0], [*argv, *project.cli_arguments], environment)
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
    # The native writer stamps the moment this context was committed for a
    # model request: the time the input was shown to the model.
    committed_at_ms: int = field(
        metadata={
            "wire_name": "committedAtMs",
            "sql": Column(check="committed_at_ms BETWEEN 1 AND 9007199254740991"),
        }
    )

    @classmethod
    def from_events(cls, input_id, session_id, input_event, context_event):
        """Strictly join the two emitted records through the context declaration.

        This is an emitted receipt, not proof of durable inclusion. A journal
        read must independently corroborate the same complete located record.
        """
        record = FieldCodec.decode(
            cls,
            {
                item.metadata.get("wire_name", item.name): getattr(context_event, item.name)
                for item in fields(cls)
            },
        )
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
        if not 0 < self.committed_at_ms <= 2**53 - 1:
            raise ValueError("Native context commit time is outside its native range")

    def at(self, session_file: Path) -> NativeContextProof:
        """Locate recorded facts; this does not grant acceptance or replay."""
        return NativeContextProof(
            self.input_id,
            self.session_id,
            self.session_entry_id,
            self.request_generation,
            self.llm_context_digest,
            self.committed_at_ms,
            session_file,
        )


@dataclass(frozen=True, slots=True)
class NativeContextJournal(NativeContextRecord, TypedTable):
    """One proof declaration owns wire facts and the indexed durable journal."""

    schema: Literal[2]
    type: Literal["context_committed"]

    without_rowid = True
    indexes = (Index(("request_generation",)), Index(("session_id",)))

    @classmethod
    def trigger_bodies(cls) -> dict[str, str]:
        table = cls.declared_name
        return {
            f"{table}_append": f""" BEFORE INSERT ON {table}
            WHEN NEW.request_generation < COALESCE((SELECT MAX(request_generation) FROM {table}), 0)
              OR EXISTS(SELECT 1 FROM {table} WHERE request_generation=NEW.request_generation
                        AND (llm_context_digest != NEW.llm_context_digest
                             OR committed_at_ms != NEW.committed_at_ms))
              OR NEW.session_id != (SELECT session_id FROM {table} ORDER BY session_id LIMIT 1)
            BEGIN SELECT RAISE(ABORT,'native proof lineage differs'); END""",
            **{
                f"{table}_{action.lower()}": (
                    f" BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'native proof is append only'); END"
                )
                for action in ("UPDATE", "DELETE")
            },
        }

    @classmethod
    def native_contract(cls) -> dict[str, Any]:
        """Generate the native writer's SQLite contract from this declaration."""
        table = cls.declared_name
        return {
            "objects": cls.schema_objects(),
            "columns": [
                {"name": item.name, "wire": item.metadata.get("wire_name", item.name)}
                for item in fields(cls)
            ],
            "insert": cls.insertion_statement(),
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
        path = NativeSessionFiles(Path(session_file)).input_proof
        with ExitStack() as custody:
            try:
                held = custody.enter_context(
                    closing(
                        os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
                    )
                )
                before = os.fstat(held.fileno())
                PrivateFileRole.require(before)
                if before.st_nlink != 1:
                    raise NativePiUnavailable("Native proof must be private and unaliased")
                db = custody.enter_context(closing(CoordinationStore.recovering_snapshot(path)))
                actual = SQLiteSchemaObject.read(
                    db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")
                )
                if SQLiteSchemaObject.differences(actual, cls.schema_objects()):
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
        return (
            self.read_evidence(
                self.session_file,
                input_id,
                request_generation=self.request_generation,
                evidence=evidence,
            )
            == self
        )

    @classmethod
    def read_evidence(
        cls,
        session_file: Path,
        input_id: str,
        *,
        request_generation: int | None = None,
        evidence: NativeEvidenceRead | None = None,
    ) -> NativeContextProof:
        """Corroborate live recorded events; parsed bytes alone grant no authority."""
        NativeInputIdText.decode(input_id)
        session_file = Path(session_file).absolute()
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


class NativePiTerminalFailure(NativePiUnavailable):
    """A proved input ended in a failed terminal and its owned process was reaped."""

    def __init__(
        self,
        detail: str,
        context: NativeContextProof,
        provider: str,
        model: str,
        failure: TurnFailure,
    ):
        super().__init__(f"Native Pi assistant did not finish successfully: {detail}")
        self.context = context
        self.provider = provider
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
            f"{provider}/{model}: {match.group(1) if match else 'request failed'}."
        )


@dataclass(frozen=True, slots=True)
class NativeTurnResult:
    def require_publishable(self) -> None:
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
    runtime: LaunchRuntime = field(kw_only=True)
    project: NativeProject = field(kw_only=True)

    @contextmanager
    def bound_arguments(self):
        """Keep the original socket directory borrowed for this child lifetime."""
        with self.project.bound_arguments() as arguments:
            yield (*self.argv, *arguments)

    @classmethod
    def bootstrap(
        cls, cli, arguments: NativeArguments, cwd, environment, configuration: RestartEnvironment,
        runtime: LaunchRuntime, *, session_context: tuple[ContextSegment, ...],
        project: NativeProject,
    ):
        """Load required integration independently of optional user discovery."""
        context_text = TurnContext.render_segments(session_context).text
        if not context_text.strip():
            raise NativePiUnavailable("Native launch requires nonempty session context")
        try:
            instructions = OWNER_INSTRUCTIONS.resolve(strict=True)
        except OSError as error:
            raise NativePiUnavailable("Canonical owner instruction asset is unavailable") from error
        env = dict(environment)
        for name in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"):
            env.pop(name, None)
        env["NODE_DISABLE_COMPILE_CACHE"] = "1"
        env["PI_WORKTREE"] = str(cwd)
        env = runtime.environment(env)
        env.update(configuration.encode_native())
        argv = (
            "node",
            "--no-global-search-paths",
            "--import",
            str(cli.with_name("agent-comms-import-fence.mjs")),
            "--import",
            str(cli.with_name("agent-comms-project-bootstrap.mjs")),
            str(cli),
            *arguments.with_extensions(
                NativeRequestTools.native_extension(cli.parent.parent),
                project.native_extension(cli.parent.parent),
            ).argv,
            *(argument for segment in session_context for argument in segment.native_arguments()),
            "--append-system-prompt",
            str(instructions),
            "--append-system-prompt",
            context_text,
        )
        return argv, env

    @classmethod
    def _build(cls, cli, arguments, cwd, environment, session, package, configuration,
               runtime: LaunchRuntime, *, session_context: tuple[ContextSegment, ...],
               project: NativeProject):
        argv, env = cls.bootstrap(cli, arguments, cwd, environment, configuration, runtime,
                                 session_context=session_context, project=project)
        return cls(argv, cwd, env, session, package, configuration=configuration, runtime=runtime,
                   project=project)

    @classmethod
    def package_for_runtime(cls, runtime: LaunchRuntime, package: Path) -> Path:
        """Acquire the explicitly selected installation and SDK artifact."""
        runtime.require_available()
        package = Path(package).absolute()
        _trusted_package(package)
        return package

    @classmethod
    def login(
        cls, runtime: LaunchRuntime, *, package: Path, worktree: Path, environment: dict[str, str]
    ) -> NativePiRpcLaunch:
        """Explicit provider UI has doctrine, not an invented registered owner."""
        package = cls.package_for_runtime(runtime, package)
        cwd = worktree.resolve(strict=True)
        if not cwd.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        arguments = NativeArguments((
            NoSessionArgument(), NoToolsArgument(),
            ExternalArgument(("--no-extensions",)),
            ExtensionArgument(str(package / "dist/pi_login_control.mjs")),
            ExternalArgument(("--no-skills", "--no-context-files",
                              "--no-prompt-templates", "--no-themes")),
        ))
        return cls._build(
            package / "dist/cli.js",
            arguments,
            cwd,
            environment,
            SelectedSession(cwd),
            package,
            RestartEnvironment.inherit(environment),
            runtime,
            session_context=(DoctrineSegment.capture(),),
            project=UnmanagedNativeProject(),
        )

    @classmethod
    def catalog(
        cls,
        runtime: LaunchRuntime,
        arguments: NativeArguments,
        *,
        package: Path,
        worktree: Path,
    ) -> NativePiRpcLaunch:
        """Metadata-only RPC owns an in-memory session, never a saved selection.

        Validate the configured arguments as managed RPC first. Only this
        launch owner supplies the native no-persistence choice; callers cannot
        replace a managed participant's selected source through CLI options.
        """
        try:
            arguments = arguments.rpc()
        except ValueError as error:
            raise NativePiUnavailable(str(error)) from error
        package = cls.package_for_runtime(runtime, package)
        cwd = worktree.resolve(strict=True)
        if not cwd.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        return cls._managed(package, arguments.extended(
            ExternalArgument(("--no-extensions", "--no-skills", "--no-context-files")),
            NoSessionArgument()), cwd, None, SelectedSession(cwd), runtime,
            session_context=(DoctrineSegment.capture(),), project=UnmanagedNativeProject())

    @classmethod
    def managed(
        cls,
        runtime: LaunchRuntime,
        arguments: NativeArguments,
        *,
        package: Path,
        worktree: Path,
        session_context: tuple[ContextSegment, ...],
        project: NativeProject,
        environment: dict[str, str] | None = None,
        session_file: str | None = None,
    ) -> NativePiRpcLaunch:
        """Prepare managed ACP/headless execution from its selected runtime."""
        try:
            arguments = arguments.rpc()
        except ValueError as error:
            raise NativePiUnavailable(str(error)) from error
        package = cls.package_for_runtime(runtime, package)
        cwd = worktree.resolve(strict=True)
        if not cwd.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        saved = Path(session_file).absolute() if session_file is not None else None
        directory = saved.parent if saved is not None else cwd
        session = (SavedSelectedSession(directory,
            identity=SessionIdentityHelper.locate(package, str(saved)))
            if saved is not None else SelectedSession(directory))
        return cls._managed(package, arguments, cwd, environment, session, runtime,
                            session_context=session_context, project=project)

    @classmethod
    def _managed(cls, package, arguments, cwd, environment, session, runtime: LaunchRuntime,
                 *, session_context: tuple[ContextSegment, ...], project: NativeProject):
        """Assemble the entire key from one acquired package and selected source."""
        cwd = Path(cwd).resolve(strict=True)
        if not cwd.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        env = dict(os.environ)
        env.update(environment or {})
        if session.path is not None:
            arguments = arguments.extended(SessionArgument(str(session.path)))
        cli = package / "dist" / "cli.js"
        return cls._build(cli, arguments, cwd, env, session, package,
                          RestartEnvironment.inherit(env), runtime, session_context=session_context,
                          project=project)

    def retained_managed(
        self,
        runtime: LaunchRuntime,
        arguments: NativeArguments,
        *,
        package: Path,
        worktree: Path,
        environment: dict[str, str] | None,
        session_file: str | None,
        session_context: tuple[ContextSegment, ...],
        project: NativeProject,
    ) -> NativePiRpcLaunch | None:
        """Derive a candidate from this acquired immutable launch, not a path cache.

        Only original child custody may consume it after comparing the complete
        launch/auth key and checking its actual saved-source revision/liveness.
        A different package or selected file needs its own fresh acquisition.
        """
        saved = Path(session_file).absolute() if session_file is not None else None
        if runtime != self.runtime or package != self.package or saved != self.session.path:
            return None
        return self._managed(self.package, arguments.rpc(),
            worktree, environment, self.session, self.runtime, session_context=session_context,
            project=project)

    @classmethod
    def acquire_tracked(cls, package: Path, runtime: LaunchRuntime,
                        arguments: NativeArguments) -> Callable[..., NativePiRpcLaunch]:
        """Acquire one immutable artifact's executable construction behavior.

        The returned factory retains the original verified CLI for its owner's
        lifetime. Every call still constructs a new complete source/auth launch;
        it grants no child readiness, input admission or prior context proof.
        """
        package = Path(package).absolute()
        return cls.verified_launcher(package, _trusted_package(package), runtime, arguments)

    @classmethod
    def verified_launcher(cls, package: Path, cli: Path, runtime: LaunchRuntime,
                          arguments: NativeArguments) -> Callable[..., NativePiRpcLaunch]:
        """Launcher for a package whose CLI _trusted_package already returned."""
        return partial(cls._tracked, Path(package).absolute(), cli, runtime, arguments)

    @classmethod
    def tracked(cls, package: Path, *, runtime: LaunchRuntime,
                arguments: NativeArguments, **options) -> NativePiRpcLaunch:
        """Acquire a fresh artifact for a standalone tracked execution."""
        return cls.acquire_tracked(package, runtime, arguments)(**options)

    @classmethod
    def _tracked(
        cls,
        package: Path,
        cli: Path,
        runtime: LaunchRuntime,
        native_arguments: NativeArguments,
        *,
        worktree: Path,
        session: SelectedSession,
        provider: str,
        model: str,
        session_context: tuple[ContextSegment, ...],
        project: NativeProject,
        thinking_level: str | None = None,
        environment: dict[str, str] | None = None,
        selected_tool_mode: NativeToolMode | None = None,
    ) -> NativePiRpcLaunch:
        """Construct a complete launch through its acquired immutable artifact.

        A backend must explicitly consume this launch, not guess Pi from a basename
        or trust an RPC capability response from an arbitrary executable. This
        only establishes the executable and its resources; native input, context,
        and model-delivery proofs remain separate per-attempt observations.
        """
        session_dir, session_file = session.directory, session.path
        configured = native_arguments.rpc()
        try:
            for value in (provider, model):
                FieldCodec.decode(str, value)
                if re.fullmatch(r"[^\s-]\S*", value) is None:
                    raise ValueError("Provider/model must be single non-option tokens")
        except (TypeError, ValueError) as error:
            raise NativePiUnavailable(
                "Native Pi requires an explicit provider and model"
            ) from error
        if selected_tool_mode is not None and not isinstance(selected_tool_mode, NativeToolMode):
            raise NativePiUnavailable("Selected tool requires a trusted nominal mode")
        session.require_launch_tools(selected_tool_mode)
        worktree = Path(worktree).absolute()
        session_dir = Path(session_dir).absolute()
        NativeSessionFiles.prepare_directory(session_dir)
        if not worktree.is_dir():
            raise NativePiUnavailable("Native Pi worktree is unavailable")
        session.require_launch_header()
        # Native SettingsManager and AgentSession own tracked retry/compaction
        # policy. This directory isolates writable resources, not policy copies.
        agent_dir = session_dir / ".native-pi-agent"
        NativeSessionFiles.prepare_directory(agent_dir)
        tool_arguments = (
            selected_tool_mode.launch_arguments(package)
            if selected_tool_mode is not None
            else NativeArguments((NoToolsArgument(),))
        )
        arguments = configured.for_owned_turn().with_model(f"{provider}/{model}").with_thinking(None).rpc().extended(
            ExternalArgument(("--no-approve",)),
            *tool_arguments.entries,
            SessionDirArgument(str(session_dir)),
            *session.launch_arguments(thinking_level).entries,
        )
        if session_file is not None:
            arguments = arguments.extended(SessionArgument(str(session_file)))
        env, configuration = session.launch_environment(agent_dir, environment)
        return cls._build(cli, arguments, worktree, env, session, package,
                          configuration, runtime, session_context=session_context, project=project)


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
            raise NativePiUnavailable(
                f"Native Pi package has unsafe ancestor: {failed.declared_name}"
            )
    root = package.parents[2]
    if PrivateDirectoryRole.violation(root.lstat()) is not None:
        raise NativePiUnavailable("Disposable native Pi root must be owner-only")
    try:
        verify_native_package(package)
    except (OSError, NativePackageError) as error:
        raise NativePiUnavailable("Pinned native Pi package differs from reviewed fork") from error
    return package / "dist/cli.js"


def read_tracked_input_digest(
    session_file: Path, input_id: str, *, evidence: NativeEvidenceRead | None = None
) -> str | None:
    """Corroborating digest only; this cannot authorize recovery or input replay.

    ``None`` means the journal holds no durable commit of this input. An
    unreadable or malformed journal is not absence and raises.
    """
    NativeInputIdText.decode(input_id)
    session_file = Path(session_file).absolute()
    with NativeInputEvidenceRead.borrow(session_file, evidence) as evidence:
        _header, entries = evidence.observe()
        users = NativeEntry.tracked_users(entries)
        if input_id not in users:
            return None
        return users[input_id].message.input_digest


def _verify_context(
    session_file: Path,
    input_id: str,
    session_id: str,
    input_event: pi.InputCommitted,
    context_event: pi.ContextCommitted,
    *,
    evidence: NativeEvidenceRead | None = None,
) -> NativeContextProof:
    try:
        emitted = NativeContextRecord.from_events(
            input_id, session_id, input_event, context_event
        ).at(session_file)
    except (TypeError, ValueError) as error:
        raise NativePiUnavailable("Native Pi input/context receipt is malformed") from error
    with NativeEvidenceRead.borrow(session_file, evidence) as evidence:
        if not emitted.corroborates_input(input_id, evidence=evidence):
            raise NativePiUnavailable("Native Pi emitted an event without matching durable proof")
        return emitted


def _require_reviewed_selected_source_cli() -> None:
    """Default OFF until copied CLI startup factories are independently fenced."""
    raise NativePiUnavailable(
        "Selected first-source CLI builtins are unreviewed; no Pi startup or raw prompt"
    )


def _fresh_selected_revision(fresh: FreshPrivateSession, *, started: bool = False) -> FileRevision:
    """Exact saved inode+revision; not a provider or terminal receipt."""
    if started:
        return fresh.verify_selected_startup()
    fresh.verify_prewrite()
    info = fresh.path.lstat()
    return FileRevision.from_stat(info)
