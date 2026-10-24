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

from agent_comms.native_runtime_input import NativeRuntimeTable
from .field_codec import InputId
from .coordination_errors import IdentityConflict, RetryScheduled
from .coordinator import Coordination
from .diagnostics import (
    record_acquisition_progress,
    record_request_progress,
)
from .following_inputs import FollowingInputs
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .native_arguments import SelectedModel
from .native_input_owner import ParticipantOwner, RegistryOwner
from .native_entries import NativeEvidenceScope
from .native_pi import NativeContextProof, NativePiTerminalFailure, NativeTurnResult
from .native_prompt_binding import bind_expected_prompt
from .native_prompt_send import PromptBoundary
from .native_session_reopen import NativeSessionIdentity
from .native_startup import NativeStartup
from .native_tool_call import NativeToolMode
from .text_digest import TextDigest
from .tracked_turn import (
    NO_FOLLOWING_INPUTS,
    NO_NATIVE_OBSERVER,
    NativeObserver,
    TrackedTurnSession,
)
from .turn_context import RenderedInput
from agent_comms.turn_input_source import SelectedOriginalInput

if TYPE_CHECKING:
    from .coordinated_runtime import SelectedExecution
    from .selected_participant import SelectedParticipant
    from .selected_session import SelectedSession
from .private_send_stage import NativeSendStage


@dataclass(frozen=True, kw_only=True)
class PrivateSendClaim(PromptBoundary):
    selected: SelectedParticipant
    stage: NativeSendStage
    input_id: InputId
    token_digest: str
    prompt: RenderedInput
    session: SelectedSession
    _once: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def following_original(self, prompt: str):
        return SelectedOriginalInput.answering(prompt, self.selected.goal_permission)

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

    def verify(
        self, store: Coordination, context: NativeContextProof,
        source_reads: NativeEvidenceScope,
    ) -> None:
        self.stage.verify(
            store,
            self.participant,
            self.input_id,
            self.token_digest,
            context,
            wire_root_id=self.wire_root_id,
            prompt=self.prompt.text,
            source_reads=source_reads,
        )
        self.owner.require_running_turn(self.bus._registry)

    async def execute(
        self,
        execution: SelectedExecution,
        *,
        model: SelectedModel,
        selected_tool_mode: NativeToolMode,
        observe_event: NativeObserver = NO_NATIVE_OBSERVER,
        following: FollowingInputs = NO_FOLLOWING_INPUTS,
    ) -> NativeTurnResult:
        """Return only live corroborated proof, or settle a proved terminal failure.

        TrackedTurnSession owns the child and tool resources. Its return or exception
        follows cleanup; disconnect and unknown outcomes are never committed here.
        The raw writer keeps the single-use send token after the input is recorded.
        """
        lease = self.owner.thread.require_turn_lease()
        registry = await Coordination.run_worker(self.bus._registry.snapshot)
        try:
            result = await TrackedTurnSession.execute(
                execution.native_package,
                input_id=self.input_id,
                prompt=self.prompt.text,
                context_contributions=self.prompt.contributions,
                worktree=Path(self.owner.thread.worktree).absolute(),
                session=self.session,
                model=model,
                thinking_level=self.owner.thread.thinking_level,
                environment=self.owner.thread.native_environment(
                    self.bus.bus.path.parent, registry,
                    self.owner.thread.worktree,
                ),
                ingress=MaintenanceBarrier(self.bus._registry.store.path),
                startup=NativeStartup(
                    self.bus.bus.path.parent,
                    measurements=self.selected.cursor_delivery.measurements,
                ),
                selected_tool_mode=selected_tool_mode,
                observe_event=observe_event,
                request_observer=partial(
                    record_request_progress, self.bus.bus.path.parent, lease,
                ),
                prompt_send_boundary=self,
                execution=execution,
                following=following,
            )
        except NativePiTerminalFailure as error:
            def settle(store: Coordination, terminal: NativePiTerminalFailure = error):
                self.verify(store, terminal.context, source_reads)
                return self.stage.fail_terminal(
                    store, self.participant, self.input_id, self.token_digest,
                    terminal.context, terminal.failure,
                )

            async with self.selected.cursor_delivery.reading() as source_reads:
                settlement = await Coordination.run_async(self.store_path, settle)
            try:
                await self.stage.continue_after_failure(settlement, error.failure)
            except RetryScheduled as retry:
                raise retry from error
            raise
        finally:
            await Coordination.run_worker(partial(
                record_acquisition_progress, self.bus.bus.path.parent, lease,
                self.input_id, self.selected.cursor_delivery.measurements,
            ))
        async with self.selected.cursor_delivery.reading() as source_reads:
            await Coordination.run_read_async(
                self.store_path, lambda store: self.verify(store, result.context, source_reads),
            )
        return result

    def commit(self, store: Coordination, context: NativeContextProof) -> None:
        self.stage.commit(store, self.participant, self.input_id, self.token_digest, context)

    def _start_once(self, identity: NativeSessionIdentity) -> None:
        """Commit the exact grant under original fences, then release them.

        Stop, rename and maintenance before this grant refuse the input. After
        it, they are ordered after the permitted attempt, whose raw
        writer retains its own pipe and token. The recorded send grant remains
        the original recovery evidence; neither successful pipe completion nor
        subsequent owner loss permits replay.
        """
        with ExitStack() as authority:
            with self.selected.cursor_delivery.measurements.operation("coordinator_open"):
                store = authority.enter_context(Coordination(str(self.store_path)))
            with self.selected.cursor_delivery.measurements.operation("maintenance_ingress"):
                authority.enter_context(MaintenanceBarrier(
                    self.bus._registry.store.path
                ).allow_input())
            with self.selected.cursor_delivery.measurements.operation("registry_exclusion"):
                registry = authority.enter_context(self.bus._registry.store.reading_snapshot())
            with self.selected.cursor_delivery.measurements.operation("prompt_binding"):
                authority.enter_context(self.stage.bound_prompt(
                    store, self.input_id, self.participant, self.wire_root_id,
                    self.prompt.text,
                ))
            with self.selected.cursor_delivery.measurements.operation("coordinator_exclusive"):
                db = authority.enter_context(store.session.writing())
            with self.selected.cursor_delivery.measurements.operation("selected_source_check"):
                self.session.verify_start(identity)
            # From token consumption through COMMIT/fsync, every failure
            # remains uncertain and propagates without another send attempt.
            if not self._once.acquire(blocking=False):
                raise IdentityConflict("native send claim_scope cannot be reused")
            with self.selected.cursor_delivery.measurements.operation("claim_checks"):
                self.owner.require_snapshot(
                    registry, "recipient registry owner changed before native send"
                )
                NativeRuntimeTable.require_installed(db)
                self.participant.require(store, self.stage.recipient_lookup)
                reserved = self.stage.require_reservation(
                    db, self.input_id, self.participant, self.token_digest
                )
                self.stage.require_claim(store)
            with self.selected.cursor_delivery.measurements.operation("native_claim_record"):
                reserved.sent_owner_claimed_generation.record(
                    reserved, db, self.owner.claimed_generation, identity
                )
            # ExitStack commits and closes the database before releasing the
            # registry and bus locks. The selected claim and prompt were already
            # checked, so this step changes only their database and input records.
            # It does not publish to the bus. Do not write any prompt bytes until
            # all of these operations have finished successfully.

    async def send_prompt(self, turn: TrackedTurnSession) -> None:
        with turn.startup.measurements.operation("prompt_writer_join"):
            await turn.native.send_fenced(
                turn.command,
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
