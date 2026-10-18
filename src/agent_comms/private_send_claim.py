"""Record permission for selected input once, before the raw pipe writer runs.

Lock owners wait before the one-use token is consumed. An uncertain write
or commit never becomes a fresh send attempt.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_errors import IdentityConflict, RetryScheduled
from .coordinator import Coordination
from .diagnostics import (
    PublicationMeasurements,
    record_acquisition_progress,
)
from .following_inputs import FollowingInputs
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .native_arguments import SelectedModel
from .native_input_owner import ParticipantOwner, RegistryOwner
from .native_pi import ContextProof, NativePiTerminalFailure, NativeTurnResult
from .native_prompt_binding import bind_expected_prompt
from .native_prompt_send import PromptBoundary, send_fenced_prompt
from .native_session_reopen import NativeSessionIdentity
from .native_tool_call import NativeToolMode
from .text_digest import TextDigest
from .tracked_turn import (
    NO_FOLLOWING_INPUTS,
    NO_NATIVE_OBSERVER,
    NO_NATIVE_TOOLS,
    NativeObserver,
)
from .turn_context import RenderedInput

if TYPE_CHECKING:
    from .coordinated_runtime import SelectedExecution
    from .selected_participant import SelectedParticipant
    from .selected_session import SelectedSession
    from .tracked_turn import TrackedTurnSession
from .private_registry_guard import _require_no_private_owner_rename
from .private_send_stage import NativeSendStage


@dataclass(frozen=True, kw_only=True)
class PrivateSendClaim(PromptBoundary):
    selected: SelectedParticipant
    stage: NativeSendStage
    input_id: str
    token_digest: str
    prompt: RenderedInput
    session: SelectedSession
    _once: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _measurements: PublicationMeasurements = field(default_factory=PublicationMeasurements,
                                                  init=False, compare=False, repr=False)

    @property
    def bus(self) -> MessageBus:
        return self.selected.bus

    @property
    def following_after_seq(self) -> int:
        """The selected sources which cannot also become following input."""
        return max(assignment.wire_seq for assignment in self.stage.assignments)

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

    @classmethod
    async def reserve(
        cls,
        *,
        selected: SelectedParticipant,
        stage: NativeSendStage,
        token: str,
        prompt: RenderedInput,
        session: SelectedSession,
    ) -> PrivateSendClaim:
        """Reserve once and bind the exact prompt before any native process starts.

        A failed binding leaves the durable reservation intact; construction must
        never roll it back or make another input eligible for automatic replay.
        """
        digest = TextDigest.of(token).value
        def reserve(store):
            input_id = stage.reserve(store, selected.identity, digest)
            bind_expected_prompt(
                store,
                input_id=input_id,
                stage=stage,
                owner=selected.owner.thread,
                generation=selected.identity.generation,
                prompt=prompt.text,
            )
            return input_id

        input_id = await Coordination.run_async(selected.store.session.path, reserve)
        return cls(
            selected=selected,
            stage=stage,
            input_id=input_id,
            token_digest=digest,
            prompt=prompt,
            session=session,
        )

    def verify(self, store: Coordination, context: ContextProof) -> None:
        context.verify_selected(self.stage,
            store,
            self.participant,
            self.input_id,
            self.token_digest,
            wire_root_id=self.wire_root_id,
            prompt=self.prompt.text,
        )
        self.owner.require_running_turn(self.bus._registry)

    async def execute(
        self,
        execution: SelectedExecution,
        *,
        model: SelectedModel,
        selected_tool_mode: NativeToolMode = NO_NATIVE_TOOLS,
        observe_event: NativeObserver = NO_NATIVE_OBSERVER,
        following: FollowingInputs = NO_FOLLOWING_INPUTS,
    ) -> NativeTurnResult:
        """Return only live corroborated proof, or settle a proved terminal failure.

        TrackedTurnSession owns the child and tool resources. Its return or exception
        follows cleanup; disconnect and unknown outcomes are never committed here.
        The raw writer keeps the single-use send token after the input is recorded.
        """
        try:
            result = await self.owner.thread.backend.run_selected(
                self, execution, model=model, selected_tool_mode=selected_tool_mode,
                observe_event=observe_event, following=following,
            )
        except NativePiTerminalFailure as error:
            def settle(store: Coordination, terminal: NativePiTerminalFailure = error):
                self.verify(store, terminal.context)
                return self.stage.fail_terminal(
                    store, self.participant, self.input_id, self.token_digest,
                    terminal.context, terminal.failure,
                )

            settlement = await Coordination.run_async(self.store_path, settle)
            try:
                await self.stage.continue_after_failure(settlement, error.failure)
            except RetryScheduled as retry:
                raise retry from error
            raise
        finally:
            lease = self.owner.thread.turn_lease
            assert lease is not None
            await Coordination.run_worker(partial(
                record_acquisition_progress, self.bus.log.path.parent, lease,
                self.input_id, self._measurements,
            ))
        await Coordination.run_read_async(self.store_path, lambda store: self.verify(store, result.context))
        return result

    def commit(self, store: Coordination, context: ContextProof) -> None:
        context.commit_selected(self, store)

    def _start_once(self, identity: NativeSessionIdentity) -> None:
        """Commit the exact grant under original fences, then release them.

        Stop, rename and maintenance before this grant refuse the input. After
        it, they are ordered after the permitted attempt, whose raw
        writer retains its own pipe and token. The recorded send grant remains
        the original recovery evidence; neither successful pipe completion nor
        subsequent owner loss permits replay.
        """
        with ExitStack() as authority:
            with self._measurements.operation("coordinator_open"):
                store = authority.enter_context(Coordination(str(self.store_path)))
            with self._measurements.operation("maintenance_ingress"):
                authority.enter_context(MaintenanceBarrier(
                    self.bus._registry.store.path
                ).allow_input())
            with self._measurements.operation("registry_exclusion"):
                document = authority.enter_context(self.bus._registry.store.reading())
            with self._measurements.operation("registry_snapshot"):
                registry = document.snapshot()
            with self._measurements.operation("prompt_binding"):
                authority.enter_context(self.stage.bound_prompt(
                    store, self.input_id, self.participant, self.wire_root_id,
                    self.prompt.text, blocking=True,
                ))
            with self._measurements.operation("coordinator_exclusive"):
                db = authority.enter_context(store.session.transaction())
            with self._measurements.operation("selected_source_check"):
                self.session.verify_start(identity)
            # From token consumption through COMMIT/fsync, every failure
            # remains uncertain and propagates without another send attempt.
            if not self._once.acquire(blocking=False):
                raise IdentityConflict("native send claim_scope cannot be reused")
            with self._measurements.operation("claim_checks"):
                _require_no_private_owner_rename(self.bus.log.path.parent)
                self.owner.require_snapshot(
                    registry, "recipient registry owner changed before native send"
                )
                assert_native_runtime_schema(db)
                self.participant.require(store, self.stage.recipient_lookup)
                reserved = self.stage.require_reservation(
                    db, self.input_id, self.participant, self.token_digest
                )
                self.stage.require_claim(store)
            with self._measurements.operation("native_claim_record"):
                reserved.sent_owner_claimed_generation.record(
                    reserved, db, self.owner.claimed_generation, identity
                )
            # ExitStack commits/closes the coordinator before releasing shared registry/wire locks. No bus publication
            # resource is borrowed: the selected claim and prompt are already
            # certified originals, and this grant changes only their SQL/input
            # records. No payload byte is
            # eligible until all of that retirement has returned successfully.

    async def send_prompt(self, turn: TrackedTurnSession) -> None:
        stdin = turn.native.proc.stdin
        assert stdin is not None
        with turn.startup.measurements.operation("prompt_writer_join"):
            await send_fenced_prompt(
                stdin, turn.native.reader.encode(turn.command),
                partial(self.prompt_boundary, turn), measurements=turn.startup.measurements,
            )

    @contextmanager
    def prompt_boundary(self, turn):
        with self(turn.native.attestation.identity):
            turn.grant_prompt()
            yield

    @contextmanager
    def __call__(
        self,
        identity: NativeSessionIdentity,
    ) -> Iterator[None]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise IdentityConflict("native send claim_scope requires the isolated raw writer")
        self._start_once(identity)
        yield
