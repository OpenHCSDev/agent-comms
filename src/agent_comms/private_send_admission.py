"""One-use selected admission commits before the original raw pipe writer.

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
from .diagnostics import PublicationMeasurements, record_acquisition_progress
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .native_input_owner import ParticipantOwner, RegistryOwner
from .native_pi import NativeContextProof, NativePiTerminalFailure, NativeTurnResult
from .native_prompt_binding import bind_expected_prompt
from .native_prompt_send import PromptAdmissionBusy
from .pi_vocabulary import ThinkingLevel
from .private_path import FileRevision
from .native_session_reopen import NativeSessionIdentity
from .text_digest import TextDigest
from .tracked_turn import TrackedTurnSession
from .turn_context import RenderedInput

if TYPE_CHECKING:
    from .agent_events import AgentEvent
    from .native_attestation import ObservedAttestation
    from .pi_events import PiEvent
    from .selected_tool_broker import NativeToolMode
    from .selected_session import SelectedSession
    from .selected_participant import SelectedParticipant
from .private_registry_guard import _require_no_private_owner_rename
from .private_send_stage import NativeSendStage


@dataclass(frozen=True, kw_only=True)
class PrivateSendAdmission:
    selected: SelectedParticipant
    stage: NativeSendStage
    input_id: str
    token_digest: str
    prompt: RenderedInput
    session: SelectedSession
    _once: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _journal: CompactionJournal = field(init=False, repr=False)
    _measurements: PublicationMeasurements = field(default_factory=PublicationMeasurements,
                                                  init=False, compare=False, repr=False)

    @property
    def bus(self) -> MessageBus:
        return self.selected.bus

    @property
    def store_path(self) -> Path:
        return self.selected.store.session.path

    @property
    def wire_root_id(self) -> str:
        return self.selected.root_id

    @property
    def owner(self) -> RegistryOwner:
        return self.selected.owner

    @property
    def participant(self) -> ParticipantOwner:
        return self.selected.identity

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
        selected: SelectedParticipant,
        stage: NativeSendStage,
        token: str,
        prompt: RenderedInput,
        session: SelectedSession,
    ) -> PrivateSendAdmission:
        """Reserve once and bind the exact prompt before any native process starts.

        A failed binding leaves the durable reservation intact; construction must
        never roll it back or make another input eligible for automatic replay.
        """
        digest = TextDigest.of(token).value
        input_id = stage.reserve(selected.store, selected.identity, digest)
        bind_expected_prompt(
            selected.store,
            input_id=input_id,
            stage=stage,
            owner=selected.owner.thread,
            generation=selected.identity.generation,
            prompt=prompt.text,
        )
        return cls(
            selected=selected,
            stage=stage,
            input_id=input_id,
            token_digest=digest,
            prompt=prompt,
            session=session,
        )

    def verify(self, store: Coordination, context: NativeContextProof) -> None:
        self.stage.verify(
            store,
            self.participant,
            self.input_id,
            self.token_digest,
            context,
            wire_root_id=self.wire_root_id,
            prompt=self.prompt.text,
        )
        self.owner.require_registry(self.bus._registry)

    async def prepare_context(self, turn: TrackedTurnSession) -> None:
        """Use this original request's selected source and acquired native custody."""
        await self.session.prepare_context(self.selected, turn)

    async def execute(
        self,
        package: Path,
        *,
        provider: str,
        model: str,
        selected_tool_mode: NativeToolMode | None = None,
        observe_event: Callable[[PiEvent | AgentEvent | ObservedAttestation], Awaitable[None]] | None = None,
    ) -> NativeTurnResult:
        """Return only live corroborated proof, or settle a proved terminal failure.

        TrackedTurnSession retains child/tool custody. Its return or exception
        follows cleanup; disconnect and unknown outcomes are never committed here.
        The raw writer retains the sole one-use send token after durable admission.
        """
        with Coordination(str(self.store_path)) as store:
            try:
                result = await TrackedTurnSession.execute(
                    package,
                    input_id=self.input_id,
                    prompt=self.prompt.text,
                    context_contributions=self.prompt.contributions,
                    worktree=Path(self.owner.thread.worktree).absolute(),
                    session=self.session,
                    provider=provider,
                    model=model,
                    thinking_level=ThinkingLevel.optional_name(self.owner.thread.thinking_level),
                    environment=self.owner.thread.native_environment(
                        self.bus.log.path.parent, self.bus._registry.snapshot(),
                        self.owner.thread.worktree,
                    ),
                    maintenance_root=self.bus.log.path.parent,
                    selected_tool_mode=selected_tool_mode,
                    observe_event=observe_event,
                    prompt_send_boundary=self,
                    acquisition_measurements=self._measurements,
                )
            except NativePiTerminalFailure as error:
                self.verify(store, error.context)
                self.stage.fail_terminal()
                raise
            finally:
                lease = self.owner.thread.turn_lease
                assert lease is not None
                record_acquisition_progress(self.bus.log.path.parent, lease,
                                            self.input_id, self._measurements)
            self.verify(store, result.context)
            return result

    def commit(self, store: Coordination, context: NativeContextProof) -> None:
        self.stage.commit(store, self.participant, self.input_id, self.token_digest, context)

    def _admit_once(
        self, identity: NativeSessionIdentity, selected_runtime_revision: FileRevision | None
    ) -> None:
        """Commit the exact grant under original fences, then release them.

        Stop, rename and maintenance before this grant refuse admission. After
        it, they are ordered after the original admitted attempt, whose raw
        writer retains its own pipe and token. The durable UNKNOWN marker and
        recorded admission remain the original recovery evidence; neither
        successful pipe completion nor subsequent owner loss permits replay.
        """
        with ExitStack() as authority:
            try:
                with self._measurements.operation("coordinator_open"):
                    store = authority.enter_context(Coordination(str(self.store_path), lock_timeout=0))
                with self._measurements.operation("response_boundary"):
                    registry = authority.enter_context(_response_boundary(
                        self.bus, blocking=False, measurements=self._measurements))
                with self._measurements.operation("coordinator_exclusive"):
                    db = authority.enter_context(store.session.irreversible_admission())
                with self._measurements.operation("prompt_binding"):
                    authority.enter_context(self.stage.bound_prompt(
                        store, self.input_id, self.participant, self.wire_root_id,
                        self.prompt.text, blocking=False,
                    ))
                with self._measurements.operation("selected_source_admission"):
                    saved = self.session.admit(identity, selected_runtime_revision)
                with self._measurements.operation("raw_journal_admission"):
                    raw = authority.enter_context(
                        self._journal.private_inputs.admission(
                            saved, blocking=False, measurements=self._measurements)
                    )
            except BlockingIOError as error:
                raise PromptAdmissionBusy("Native admission exclusion is busy") from error
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise PromptAdmissionBusy("Native admission database is busy") from error
                raise
            # Busy acquisition is the ONLY repeatable operation. From token
            # consumption through COMMIT/fsync, every failure retains original
            # uncertainty and must propagate without another admission probe.
            if not self._once.acquire(blocking=False):
                raise IdentityConflict("native send admission cannot be reused")
            with self._measurements.operation("admission_checks"):
                _require_no_private_owner_rename(self.bus.log.path.parent)
                MaintenanceBarrier(self.bus._registry.store.path).assert_open_unlocked()
                self.owner.require_snapshot(
                    registry, "recipient registry owner changed before native send"
                )
                assert_native_runtime_schema(db)
                self.participant.require(store, self.stage.recipient_lookup)
                reserved = self.stage.require_reservation(
                    db, self.input_id, self.participant, self.token_digest
                )
                self.stage.require_claim(store)
            with self._measurements.operation("raw_unknown_checkpoint"):
                raw.mark_unknown(self.input_id)
            with self._measurements.operation("native_admission_record"):
                reserved.sent_owner_admission_generation.record(
                    reserved, db, self.owner.admission_generation, identity
                )
            # ExitStack commits/closes the original journal and coordinator
            # before releasing registry/bus/wire custody. No payload byte is
            # eligible until all of that retirement has returned successfully.

    @contextmanager
    def __call__(
        self,
        identity: NativeSessionIdentity,
        selected_runtime_revision: FileRevision | None = None,
    ) -> Iterator[None]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise IdentityConflict("native send admission requires the isolated raw writer")
        self._admit_once(identity, selected_runtime_revision)
        yield
