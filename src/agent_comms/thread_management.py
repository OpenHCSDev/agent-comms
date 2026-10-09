"""Thread registration and cross-store identity transactions."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_comms.coordination_contracts import MAX_IDENTIFIER_CHARS
from agent_comms.coordination_errors import IdentityConflict

from .child_process import ProcessIdentity
from .registration import Registration
from .thread_status import StoppedThreadStatus

if TYPE_CHECKING:
    pass
from .agent_activity import AgentActivity
from .catalog_store import ChannelCatalog
from .collaboration_ledger import CollaborationLedger
from .errors import RelationViolationError
from .importing import ImportFormat, ImportLimits, ImportReceipt
from .input_disposition import InputDispositions
from .message_bus import MessageBus
from .owner_lifecycle import OwnerLifecycle
from .private_registry_guard import PRIVATE_OWNER_RENAME_PENDING, _require_no_private_owner_rename
from .registry_document import RegistrySnapshot
from .store_files import _atomic_write_text, _store_lock
from .threads import Thread, current_thread

_LOG = logging.getLogger(__name__)


def _session_model(session_file: Path) -> tuple[str, str] | None:
    """Read the last model choice from a pi session file.

    Non-interactive pi requires an explicit provider/model, so forks replay the
    parent's final ``model_change`` entry. Returns ``(provider, model_id)`` or
    ``None`` when the session carries no model record.
    """
    from .native_transcript import NativeTranscript

    for entry in NativeTranscript(session_file).tail():
        if entry.model_choice is not None:
            return entry.model_choice
    return None


@dataclass(frozen=True, slots=True)
class ForkSpec:
    """Declares one fork: a child thread spawned from a parent's session."""

    name: str
    parent: str
    task: str = ""
    tags: frozenset[str] | None = None
    prompt: str | None = None

    @property
    def initial_prompt(self) -> str:
        return self.task if self.prompt is None else self.prompt




@dataclass(frozen=True, slots=True)
class RenameThreadResult:
    previous: str
    current: str
    changed: bool


@dataclass(frozen=True, slots=True)
class ProjectChangeResult:
    thread: str
    previous: str
    current: str
    changed: bool


class ThreadManagement:
    def __init__(
        self,
        root: Path,
        registry: Registration,
        bus: MessageBus,
        catalog: ChannelCatalog,
        agents: AgentActivity,
        owners: OwnerLifecycle,
        ledger: CollaborationLedger,
    ):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.catalog = catalog
        self.agents = agents
        self.owners = owners
        self.ledger = ledger
        self._wire_lock_path = root / "wire"

    def import_thread(
        self,
        source: Path | str,
        format: ImportFormat,
        *,
        name: str,
        session_id: str | None = None,
        worktree: str | None = None,
        model: str | None = None,
        tags: frozenset[str] = frozenset(),
        limits: ImportLimits | None = None,
    ) -> ImportReceipt:
        """Import a stopped, resumable snapshot without adopting a source executor."""
        source_path = Path(source).expanduser().resolve()
        snapshot = format.read(source_path, limits or ImportLimits(), session_id)
        directory = worktree or snapshot.project
        if not directory:
            raise ValueError("The source has no project directory; supply --worktree.")
        project = Path(directory).expanduser().resolve()
        if not project.is_dir():
            raise ValueError("The imported project does not exist locally; supply --worktree.")
        session_path = self.root / "imported_sessions" / f"{uuid4().hex}.jsonl"
        thread = Thread(
            name,
            tags,
            str(project),
            session_file=str(session_path),
            title=snapshot.title or name,
            model=model,
        )
        with _store_lock(self._wire_lock_path):
            if self.registry.name_reserved(name):
                raise ValueError(f"Thread name {name!r} is already reserved.")
            session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name == "posix":
                # Session output must remain in an owner-controlled directory.
                info = session_path.parent.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                ):
                    raise ValueError("Imported session directory is not owner-controlled.")
            _atomic_write_text(session_path, snapshot.pi_session(project))
            try:
                self.registry._declare_unlocked(thread, StoppedThreadStatus())
                self.bus.mark_delivered_through(name, self.bus.log.latest_sequence())
            except Exception:
                if name in self.registry:
                    self.registry.remove(name)
                session_path.unlink(missing_ok=True)
                raise
        return ImportReceipt(
            name,
            str(session_path),
            format,
            snapshot.source_id,
            len(snapshot.messages),
            snapshot.messages_seen - len(snapshot.messages),
            snapshot.truncated_messages,
            snapshot.notices,
            snapshot.historical_instructions,
        )

    def restore_stopped(self, source: RegistrySnapshot, names: Sequence[str]) -> tuple[str, ...]:
        """Restore historical declarations and their private delivery identities.

        Registry restoration owns collision checks and strips execution authority.
        On a private bus, stopped subscribers still belong to frozen audiences;
        their coordinator identities must exist before any cohort can be accepted.
        Repeating this operation repairs an interrupted coordinator registration
        without replacing existing owners or importing historical delivery state.
        """
        from .bus_publication import stable_thread_lookup
        from .coordinator import Coordination

        selected = tuple(dict.fromkeys(names))
        with _store_lock(self._wire_lock_path):
            restored = self.registry.restore_stopped(source, selected)
            with _store_lock(self.registry.store.path):
                private = self.registry.store.private_guard_unlocked() is not None
            if private:
                with Coordination(str(self.root / "coordination.sqlite3")) as store:
                    for name in selected:
                        thread = self.registry.require(name)
                        store.participants.register(
                            stable_thread_lookup(thread.created_at),
                            thread.name,
                            thread.name,
                            committed=True,
                        )
            return restored

    def claim_thread(
        self,
        base_name: str,
        *,
        tags: frozenset[str],
        worktree: str,
        pid: int = 0,
        start_at_latest: bool = False,
        model: str | None = None,
        thinking_level: str | None = None,
        auto_title_pending: bool = False,
    ) -> Thread:
        """Atomically register a unique thread and optionally baseline its inbox."""
        with _store_lock(self._wire_lock_path):
            thread = Thread(
                name=base_name,
                tags=tags,
                worktree=worktree,
                process_identity=ProcessIdentity.capture(pid) if pid > 0 else None,
                model=model,
                thinking_level=thinking_level,
                auto_title_pending=auto_title_pending,
            )
            thread = self.registry._claim_unlocked(thread)
            if start_at_latest:
                self.bus.mark_delivered_through(thread.name, self.bus.log.latest_sequence())
            return thread

    def rename_self(self, new_name: str) -> RenameThreadResult:
        """Rename the caller's own running thread, retaining its old aliases."""
        caller = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
        if not caller:
            raise RelationViolationError("Self rename requires PI_AGENT_ID or AGENT_COMMS_THREAD.")
        with _store_lock(self._wire_lock_path):
            if self.registry.require(caller).auto_title_pending and (
                len(new_name) > 48 or len(re.split(r"[-_]+", new_name)) > 6
            ):
                raise ValueError("Choose a concise topic title: at most 6 words and 48 characters.")
            return self._rename_thread_unlocked(
                caller, new_name, title=new_name.replace("-", " ").replace("_", " ")
            )

    def set_project_self(self, path: str) -> ProjectChangeResult:
        caller = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
        if not caller:
            raise RelationViolationError("Changing your project requires a thread identity.")
        return self.set_project(caller, path)

    def set_project(self, name: str, path: str) -> ProjectChangeResult:
        """Move one thread's project, preserving its owner and conversation."""
        if not path.strip():
            raise ValueError("Project path cannot be empty.")
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = Path(thread.worktree) / target
            try:
                target = target.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"Cannot open project directory: {target}") from error
            if not target.is_dir():
                raise ValueError(f"Project path is not a directory: {target}")
            previous = str(Path(thread.worktree).expanduser().resolve())
            current = str(target)
            if current == previous:
                return ProjectChangeResult(thread.name, previous, current, False)
            history = tuple(dict.fromkeys((*thread.previous_worktrees, previous)))
            self.registry.register(
                replace(thread, worktree=current, previous_worktrees=history),
                self.registry.status(thread.name),
            )
            return ProjectChangeResult(thread.name, previous, current, True)

    def rename_managed_thread(
        self, name: str, display_name: str, *, owner_pid: int
    ) -> RenameThreadResult:
        """Rename a locally managed running thread after proving process ownership."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            if owner_pid <= 0 or thread.process_identity != ProcessIdentity.capture(owner_pid):
                raise RelationViolationError(
                    f"Process {owner_pid} does not own thread {thread.name!r}."
                )
            base_name = re.sub(r"[^A-Za-z0-9_-]+", "-", display_name).strip("-_") or "session"

            new_name = base_name
            suffix = 2
            while self.registry.name_reserved(new_name):
                if self.registry.canonical_name(new_name) == thread.name:
                    return self._rename_thread_unlocked(thread.name, new_name, title=display_name)
                new_name = f"{base_name}-{suffix}"
                suffix += 1
            return self._rename_thread_unlocked(thread.name, new_name, title=display_name)

    def _rename_thread(
        self, name: str, new_name: str, *, title: str | None = None
    ) -> RenameThreadResult:
        # Keep direct callers under the same wire lock as public self/managed
        # rename. Private owner migration and ordinary publication share it.
        with _store_lock(self._wire_lock_path):
            return self._rename_thread_unlocked(name, new_name, title=title)

    def _rename_thread_unlocked(
        self, name: str, new_name: str, *, title: str | None = None
    ) -> RenameThreadResult:

        from .bus_publication import stable_thread_lookup
        from .coordinator import Coordination

        before = self.registry.require(name)
        private_meta = self.root / "bus_meta.json"
        if private_meta.is_symlink():
            raise RelationViolationError("Bus metadata cannot be a symlink.")
        private = False
        if private_meta.exists():
            with self.bus.log.locked():
                metadata = self.bus.log.read_metadata_unlocked(required=True)
                if metadata.private:
                    self.bus.log._private_marker_unlocked()
                    private = True
        if private:
            _require_no_private_owner_rename(self.root)
        coordinator = self.root / "coordination.sqlite3"
        intent = self.root / PRIVATE_OWNER_RENAME_PENDING
        intent_created = False
        if not private or not coordinator.exists() or before.name == new_name:
            previous, current = self.registry.rename(name, new_name)
        else:
            # Validate every deterministic registry/SQL name refusal *before*
            # advancing a committed private owner's generation. The wire lock
            # excludes cooperating sends until both authorities agree.
            if not self.registry.status(before.name).running:
                raise RelationViolationError("Only a running thread can rename itself.")
            if (
                self.registry.name_reserved(new_name)
                and self.registry.canonical_name(new_name) != before.name
            ):
                raise RelationViolationError(f"Thread name {new_name!r} is already in use.")
            replace(before, name=new_name)
            if len(new_name) > MAX_IDENTIFIER_CHARS:
                raise ValueError("Private coordinator owner name exceeds its bound.")
            with Coordination(str(coordinator)) as store:
                try:
                    person = store.participants.get(stable_thread_lookup(before.created_at))
                except IdentityConflict as error:
                    if str(error) != "participant aggregate is not registered":
                        raise
                    previous, current = self.registry.rename(name, new_name)
                else:
                    if not person.committed or person.owner_thread != before.name:
                        raise RelationViolationError(
                            "Private coordinator owner differs from the registered owner."
                        )
                    # Persist a fail-closed cross-store intent before SQL or
                    # registry changes. A crash leaves private publication and
                    # selected execution unavailable until manual inspection;
                    # it cannot silently turn an old attempt into a new send.
                    _atomic_write_text(
                        intent,
                        json.dumps(
                            {
                                "version": 1,
                                "lookup": person.lookup,
                                "old": before.name,
                                "new": new_name,
                                "generation": person.participant_generation,
                                "wireRootId": metadata.root_id,
                            },
                            sort_keys=True,
                        ),
                        fsync_parent=True,
                    )
                    intent_created = True
                    store.participants.advance_generation(
                        person.lookup, new_name, expected_generation=person.participant_generation
                    )
                    # An old selected attempt stays fenced in its old generation;
                    # never retry or reassign it after this ownership change.
                    try:
                        previous, current = self.registry.rename(name, new_name)
                    except BaseException as error:
                        # Registry persistence can fail after the SQL CAS. If
                        # its atomic snapshot still names the old owner, make
                        # a NEW generation for that owner instead of leaving
                        # a committed coordinator pointing at the new name.
                        # Neither generation may inherit an old native input.
                        try:
                            actual = self.registry.require(before.name).name
                            if actual == before.name:
                                store.participants.advance_generation(
                                    person.lookup,
                                    before.name,
                                    expected_generation=person.participant_generation + 1,
                                )
                        except BaseException:
                            pass  # ambiguous dual-store failure needs manual inspection
                        raise RelationViolationError(
                            "Private owner rename is uncertain; inspect both authorities."
                        ) from error
        thread = self.registry.require(current)
        if thread.auto_title_pending or title is not None:
            self.registry.register(
                replace(thread, auto_title_pending=False, title=title or thread.title),
                self.registry.status(current),
            )
        if previous == current:
            return RenameThreadResult(previous, current, False)
        self.agents.activity.rename_thread(previous, current)
        self.agents.runtime_info.rename_thread(previous, current)
        self.ledger.rename_thread(previous, current)
        with self.catalog.editing() as document:
            document.rename_thread(previous, current)
        if intent_created:
            # Persist completion only after both authorities and ancillary
            # stores agree. If the subsequent unlink/fsync is uncertain, the
            # previous durable record already proves a finished transition;
            # an extant file still blocks all new private sends.
            completed_intent = json.loads(intent.read_text())
            completed_intent["completed"] = True
            _atomic_write_text(
                intent, json.dumps(completed_intent, sort_keys=True), fsync_parent=True
            )
            intent.unlink()
            directory_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return RenameThreadResult(previous, current, True)

    def heartbeat(self, name: str) -> None:
        with _store_lock(self._wire_lock_path):
            self.registry.heartbeat(name)

    def set_thread_configuration(
        self, original: Thread, *, model: str | None = None,
        thinking_level: str | None = None,
    ) -> Thread:
        """Publish next-turn settings together against their acquired declaration.

        Progress and goals belong to the current document. A changed selection
        or project invalidates the native settings result acquired by the caller.
        """
        Thread.require_declaration(original)
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(original.name)
            if (thread.incarnation, thread.worktree, thread.model, thread.thinking_level) != (
                original.incarnation, original.worktree, original.model, original.thinking_level,
            ):
                raise RelationViolationError("Thread configuration changed before publication")
            thread.execution.require_native()
            updated = replace(
                thread,
                model=thread.model if model is None else model.strip(),
                thinking_level=thread.thinking_level if thinking_level is None else thinking_level,
            )
            if updated != thread:
                self.registry.register(updated, self.registry.status(thread.name))
            return updated

    def initialize_native_configuration(
        self, name: str, *, model: str | None, thinking_level: str | None
    ) -> Thread:
        """Persist one first-known producer observation through its declaration."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            updated = thread.initialized_native_configuration(
                model=model, thinking_level=thinking_level
            )
            if updated != thread:
                self.registry.register(updated, self.registry.status(thread.name))
            return updated

    def resolve_thread_model(self, name: str, default: str | None = None) -> str | None:
        """Prefer a saved selection, then the resumed session's last model."""
        thread = self.registry.require(name)
        if thread.model is not None:
            return thread.model
        if thread.session_file and (model := _session_model(Path(thread.session_file))):
            return "/".join(model)
        info = self.agents.agent_info_of(thread.name)
        return (info.model if info else None) or default

    def attach_session(self, original: Thread, session_file: str, *, pid: int | None = None) -> Thread:
        """Attach authoritative Pi runtime state to an existing thread."""
        with _store_lock(self._wire_lock_path):
            current = self.registry.require(original.name)
            if current != original:
                raise RelationViolationError("Original thread changed before native source publication")
            attached = replace(
                current,
                process_identity=(
                    current.process_identity
                    if pid is None
                    else ProcessIdentity.capture(pid)
                    if pid > 0
                    else None
                ),
                session_file=str(Path(session_file).expanduser().resolve()),
            )
            self.registry.register(attached)
            return attached

    def archive(self, name: str) -> None:
        """Hide a stopped participant from presence while retaining messages."""
        with _store_lock(self._wire_lock_path):
            self._archive_unlocked((self.registry.require(name),))

    def _archive_unlocked(self, originals: Sequence[Thread]) -> None:
        self.registry.archive_originals(originals)
        for original in originals:
            self.agents.runtime_info.remove(original.name)

    def _delete_unlocked(self, originals: Sequence[Thread]) -> None:
        """Remove stopped declarations, preserving histories and uncertain inputs."""
        self.registry.delete_originals(originals)
        with self.catalog.editing() as document:
            for original in originals:
                document.remove_thread(original.name)
        for original in originals:
            self.agents.runtime_info.remove(original.name)


    def fork(self, spec: ForkSpec, pi_bin: str | None = None) -> Thread:
        """Fork with the current owner's backend executable unless overridden.

        The native fork copies the parent's whole saved history (the SDK holds
        both session writer locks and fsyncs). It runs before the global wire
        lock, in the same writer-then-wire order as other native source edits,
        so a large parent no longer blocks every owner on the root. The locked
        declaration re-checks the parent and name against the captured source.
        """
        from .compaction_journal import CompactionJournal
        from .native_fork import fork_native_session

        resolved_bin = pi_bin or os.environ.get("AGENT_COMMS_AGENT_BIN", "pi")
        parent = self._fork_parent(spec)
        session = fork_native_session(parent.session_file, parent.worktree, resolved_bin,
            private_inputs=CompactionJournal(self.root / "compaction-commits.sqlite3").private_inputs)
        with _store_lock(self._wire_lock_path):
            return self._fork_unlocked(spec, resolved_bin, session)

    def _fork_parent(self, spec: ForkSpec) -> Thread:
        parent = self.registry.require(spec.parent)
        parent.execution.require_native()
        if self.registry.name_reserved(spec.name):
            raise RelationViolationError(
                f"Thread {spec.name!r} already exists; reuse it instead of forking it again."
            )
        if not parent.session_file:
            raise RelationViolationError(
                f"Parent thread {spec.parent!r} has no session file to fork."
            )
        return parent

    def _fork_unlocked(self, spec: ForkSpec, pi_bin: str, session) -> Thread:
        """Declare and launch the forked child under the wire lock.

        Proves the parent is still registered with the forked source and the
        name is still free, declares the child, registers it, then launches the
        subprocess. Fail-closed: if the launch fails the registration is rolled
        back. A lost race leaves the already-created fork unregistered.
        """
        parent = self._fork_parent(spec)
        try:
            session.source.require_session(parent.session_file)
        except ValueError as error:
            raise RelationViolationError(
                f"Parent thread {spec.parent!r} changed its saved session during the fork; "
                f"the created fork {session.session_file} was not registered."
            ) from error
        child = Thread(
            name=spec.name,
            tags=parent.tags if spec.tags is None else spec.tags,
            worktree=parent.worktree,
            parent=spec.parent,
            task=spec.task,
            session_file=session.session_file,
            process_identity=None,
            model=parent.model,
            thinking_level=parent.thinking_level,
        )
        child = self.registry._declare_unlocked(child)
        self.bus.mark_delivered_through(child.name, self.bus.log.latest_sequence())

        key = f"acp:{uuid4().hex}" if spec.initial_prompt else None
        try:
            owned = self.owners._launch_owner_unlocked(
                replace(child, model=self.resolve_thread_model(child.name)),
                pi_bin, startup_input_key=key,
            )
            if key is not None:
                InputDispositions(self.root / InputDispositions.filename).record(
                    key, seq=None, owner=owned.name, target=owned.name,
                    admission=self.registry.snapshot().admission_generations[owned.name],
                    text=spec.initial_prompt,
                )
            return owned
        except OSError:
            self.registry.remove(spec.name)
            raise

    def adopt_current(self) -> Thread:
        """Declare and register the current process's thread from env."""
        thread = current_thread()
        return self.registry.declare(thread)
