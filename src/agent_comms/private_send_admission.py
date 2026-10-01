"""One-use selected native admission held through every raw pipe write.

Only exclusion acquisition is repeatable. A taken token, durable UNKNOWN marker,
raw byte or uncertain commit never becomes a fresh send attempt.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .compaction_journal import CompactionJournal
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_errors import IdentityConflict
from .coordination_response import _response_boundary
from .coordinator import Coordination
from .fresh_private_session import FreshPrivateSession
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .native_input_owner import ParticipantOwner, RegistryOwner
from .native_pi import NativeContextProof, NativePiTerminalFailure, NativeTurnResult
from .native_prompt_binding import bind_expected_prompt
from .native_prompt_send import PromptAdmissionBusy
from .pi_vocabulary import ThinkingLevel
from .private_path import FileRevision
from .text_digest import TextDigest
from .tracked_turn import TrackedTurnSession

if TYPE_CHECKING:
    from .agent_events import AgentEvent
    from .pi_events import PiEvent
    from .selected_tool_broker import NativeToolMode
from .private_registry_guard import _require_no_private_owner_rename
from .private_send_stage import NativeSendStage
from .registry_document import RegistrySnapshot


@dataclass(frozen=True, kw_only=True)
class PrivateSendAdmission:
    bus: MessageBus
    store_path: Path
    wire_root_id: str
    owner: RegistryOwner
    participant: ParticipantOwner
    stage: NativeSendStage
    input_id: str
    token_digest: str
    prompt: str
    expected_session: Path | None
    fresh_selected: FreshPrivateSession | None
    _once: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _journal: CompactionJournal = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Prepare schema before the deadline-constrained raw writer, as before.
        object.__setattr__(
            self,
            "_journal",
            CompactionJournal(self.bus.log.path.parent / "compaction-commits.sqlite3"),
        )

    @classmethod
    def reserve(
        cls,
        *,
        bus: MessageBus,
        store: Coordination,
        wire_root_id: str,
        owner: RegistryOwner,
        participant: ParticipantOwner,
        stage: NativeSendStage,
        token: str,
        prompt: str,
        expected_session: Path | None,
        fresh_selected: FreshPrivateSession | None,
    ) -> PrivateSendAdmission:
        """Reserve once and bind the exact prompt before any native process starts.

        A failed binding leaves the durable reservation intact; construction must
        never roll it back or make another input eligible for automatic replay.
        """
        digest = TextDigest.of(token).value
        input_id = stage.reserve(store, participant, digest)
        bind_expected_prompt(
            store,
            input_id=input_id,
            stage=stage,
            owner=owner.thread,
            generation=participant.generation,
            prompt=prompt,
        )
        return cls(
            bus=bus,
            store_path=store.session.path,
            wire_root_id=wire_root_id,
            owner=owner,
            participant=participant,
            stage=stage,
            input_id=input_id,
            token_digest=digest,
            prompt=prompt,
            expected_session=expected_session,
            fresh_selected=fresh_selected,
        )

    @property
    def session_dir(self) -> Path:
        return self.bus.log.path.parent / "native-sessions" / self.stage.assignment.recipient_lookup

    def verify(self, store: Coordination, context: NativeContextProof) -> None:
        self.stage.verify(
            store,
            self.participant,
            self.input_id,
            self.token_digest,
            context,
            session_dir=self.session_dir,
            wire_root_id=self.wire_root_id,
            prompt=self.prompt,
        )
        self.owner.require_registry(self.bus._registry)

    async def execute(
        self,
        package: Path,
        *,
        provider: str,
        model: str,
        selected_tool_mode: NativeToolMode | None = None,
        observe_event: Callable[[PiEvent | AgentEvent], Awaitable[None]] | None = None,
    ) -> NativeTurnResult:
        """Return only live corroborated proof, or settle a proved terminal failure.

        TrackedTurnSession retains child/tool custody. Its return or exception
        follows cleanup; disconnect and unknown outcomes are never committed here.
        The raw writer still owns the sole one-use send token and exclusion.
        """
        with Coordination(str(self.store_path)) as store:
            try:
                result = await TrackedTurnSession.execute(
                    package,
                    input_id=self.input_id,
                    prompt=self.prompt,
                    worktree=Path(self.owner.thread.worktree).absolute(),
                    session_dir=self.session_dir,
                    session_file=self.expected_session,
                    provider=provider,
                    model=model,
                    thinking_level=ThinkingLevel.optional_name(self.owner.thread.thinking_level),
                    maintenance_root=self.bus.log.path.parent,
                    fresh_selected=self.fresh_selected,
                    selected_tool_mode=selected_tool_mode,
                    observe_event=observe_event,
                    prompt_send_boundary=self,
                )
            except NativePiTerminalFailure as error:
                self.verify(store, error.context)
                self.stage.fail_terminal()
                raise
            self.verify(store, result.context)
            return result

    def commit(self, store: Coordination, context: NativeContextProof) -> None:
        self.stage.commit(store, self.participant, self.input_id, self.token_digest, context)

    @contextmanager
    def _exclusion(self) -> Iterator[tuple[Coordination, RegistrySnapshot, sqlite3.Connection]]:
        with ExitStack() as authority:
            try:
                store = authority.enter_context(Coordination(str(self.store_path), lock_timeout=0))
                registry = authority.enter_context(_response_boundary(self.bus, blocking=False))
                db = authority.enter_context(store.session.irreversible_admission())
            except BlockingIOError as error:
                raise PromptAdmissionBusy("Native admission exclusion is busy") from error
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise PromptAdmissionBusy("Native admission database is busy") from error
                raise
            yield store, registry, db

    def _saved_session(
        self, actual: Path, runtime_revision: FileRevision | None
    ) -> Path:
        # get_state resolved this exact source before the writer was started.
        if (
            not isinstance(actual, Path)
            or actual.is_symlink()
            or (
                self.expected_session is not None
                and (actual != self.expected_session or not actual.is_file())
            )
        ):
            raise IdentityConflict("native send requires an exact saved session file")
        try:
            saved = actual.resolve(strict=False)
            directory = (
                self.bus.log.path.parent
                / "native-sessions"
                / self.stage.assignment.recipient_lookup
            ).resolve(strict=True)
        except OSError as error:
            raise IdentityConflict("native saved session unavailable before send") from error
        if (
            saved.parent != directory
            or saved.suffix != ".jsonl"
            or (actual.exists() and not actual.is_file())
        ):
            raise IdentityConflict("native saved session changed before send")
        if self.fresh_selected is not None and (
            runtime_revision is None
            or saved != self.fresh_selected.path
            or self.fresh_selected.verify_selected_startup() != runtime_revision
        ):
            raise IdentityConflict("selected fresh source changed before native send")
        return saved

    @contextmanager
    def __call__(
        self,
        actual_session_file: Path,
        selected_runtime_revision: FileRevision | None = None,
    ) -> Iterator[None]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise IdentityConflict("native send admission requires the isolated raw writer")
        with self._exclusion() as (store, registry, db):
            if not self._once.acquire(blocking=False):
                raise IdentityConflict("native send admission cannot be reused")
            _require_no_private_owner_rename(self.bus.log.path.parent)
            MaintenanceBarrier(self.bus._registry.store.path).assert_open_unlocked()
            self.owner.require_snapshot(
                registry, "recipient registry owner changed before native send"
            )
            assert_native_runtime_schema(db)
            self.participant.require(store, self.stage.assignment.recipient_lookup)
            reserved = self.stage.require_reservation(
                db, self.input_id, self.participant, self.token_digest
            )
            self.stage.require_claim(store)
            self.stage.require_binding(
                store, self.input_id, self.participant, self.wire_root_id, self.prompt
            )
            saved = self._saved_session(actual_session_file, selected_runtime_revision)
            # Persist UNKNOWN before any byte. Then retain the SAME journal's
            # exclusion through the raw writer; neither ACK nor fake result clears it.
            self._journal.private_inputs.reserve(saved, self.input_id)
            with self._journal.private_inputs.send_fence(saved, private_input_id=self.input_id):
                reserved.sent_owner_admission_generation.record(
                    reserved, db, self.owner.admission_generation
                )
                yield
