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

from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import IdentityConflict, RetryScheduled
from agent_comms.execution.diagnostics import record_acquisition_progress, record_request_progress
from agent_comms.execution.following_input_contracts import FollowingPromptBoundary
from agent_comms.execution.goal_turn_permit import NoLaunchPermit
from agent_comms.execution.input_drain import InputDrain
from agent_comms.execution.native_arguments import SelectedModel
from agent_comms.execution.native_entries import NativeEvidenceScope
from agent_comms.execution.native_execution import PackagedNativeExecution
from agent_comms.execution.native_input_owner import RegistryOwner
from agent_comms.execution.native_input_proof import NativeContextProof
from agent_comms.execution.native_pi import NativeTurnResult
from agent_comms.execution.native_prompt_binding import bind_expected_prompt
from agent_comms.execution.native_runtime_input import NativeRuntimeTable
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.execution.native_startup import NativeStartup
from agent_comms.execution.owned_send import OwnedSend
from agent_comms.execution.private_send_stage import NativeSendStage
from agent_comms.execution.publication_measurements import PublicationMeasurements
from agent_comms.execution.selected.participant import SelectedParticipant
from agent_comms.execution.selected.session import SelectedSession
from agent_comms.execution.tracked_turn import (
    NO_FOLLOWING_INPUTS,
    NO_NATIVE_OBSERVER,
    NativeObserver,
    TrackedTurnSession,
)
from agent_comms.execution.turn_context import RenderedInput
from agent_comms.execution.turn_failure import NativePiTerminalFailure
from agent_comms.execution.turn_input_source import SelectedOriginalInput
from agent_comms.following_inputs import FollowingInputs
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.nominal.field_codec import AcpSessionId, InputId, RootId
from agent_comms.nominal.text_digest import TextDigest
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier
from agent_comms.transport.native_tool_transport import NativeToolMode
from agent_comms.transport.project_request import ProjectRuntimeRequest


@dataclass(frozen=True, kw_only=True)
class PrivateSendClaim(FollowingPromptBoundary):
    selected: SelectedParticipant
    stage: NativeSendStage
    input_id: InputId
    token_digest: str
    prompt: RenderedInput
    session: SelectedSession
    measurements: PublicationMeasurements = field(repr=False, compare=False)
    _once: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def following_sender(self, inputs: InputDrain, session_id: AcpSessionId, prompt: str) -> OwnedSend:
        """The committed prompt supplies following authority, never a later selection."""
        return OwnedSend(
            comms=inputs.comms,
            inputs=inputs,
            goal_store=inputs.comms.goals.open_goal_store(),
            session_id=session_id,
            thread=self.selected.owner.thread,
            turn=self.selected.owner.turn_lease.turn_id,
            claim_generation=self.selected.owner.claimed_generation,
            original_source=SelectedOriginalInput.answering(prompt, self.selected.goal_permission),
            goal_permit=NoLaunchPermit(),
        )


    @property
    def following_after_seq(self) -> int:
        """The selected sources which cannot also become following input."""
        return max(assignment.wire_seq for assignment in self.stage.assignments)

    @property
    def store_path(self) -> Path:
        return self.selected.store.session.path


    @classmethod
    async def reserve(
        cls,
        *,
        selected: SelectedParticipant,
        stage: NativeSendStage,
        token: str,
        prompt: RenderedInput,
        session: SelectedSession,
        attempt: int = 0,
    ) -> PrivateSendClaim:
        """Reserve once and bind the exact prompt before any native process starts.

        A failed binding leaves the durable reservation intact; construction must
        never roll it back or make another input eligible for automatic replay.
        """
        measurements = PublicationMeasurements()
        digest = TextDigest.of(token).value
        def reserve(store):
            with measurements.operation("input_reservation_binding_worker"):
                input_id = stage.reserve(store, selected.identity, digest, attempt=attempt)
                bind_expected_prompt(
                    store,
                    input_id=input_id,
                    stage=stage,
                    owner=selected.owner.thread,
                    generation=selected.identity.generation,
                    prompt=prompt.text,
                )
                return input_id

        with measurements.operation("input_reservation_binding"):
            input_id = await Coordination.run_async(selected.store.session.path, reserve)
        return cls(
            selected=selected,
            stage=stage,
            input_id=input_id,
            token_digest=digest,
            prompt=prompt,
            session=session,
            measurements=measurements,
        )

    def verify(
        self, store: Coordination, context: NativeContextProof,
        source_reads: NativeEvidenceScope,
    ) -> None:
        self.stage.verify(
            store,
            self.selected.identity,
            self.input_id,
            self.token_digest,
            context,
            wire_root_id=self.selected.root_id,
            prompt=self.prompt.text,
            source_reads=source_reads,
        )
        self.selected.owner.require_running_turn(self.selected.bus._registry)

    async def execute(
        self,
        execution: PackagedNativeExecution,
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
        lease = self.selected.owner.thread.require_turn_lease()
        startup = NativeStartup(self.selected.bus.bus.path.parent, measurements=self.measurements)
        def snapshot_registry() -> RegistrySnapshot:
            with self.measurements.operation("selected_registry_snapshot_worker"):
                return self.selected.bus._registry.snapshot()

        try:
            with self.measurements.operation("selected_registry_snapshot"):
                registry = await Coordination.run_worker(snapshot_registry)
            result = await TrackedTurnSession.execute(
                execution.native_package,
                input_id=self.input_id,
                prompt=self.prompt.text,
                context_contributions=self.prompt.contributions,
                worktree=self.selected.owner.thread.worktree.path.absolute(),
                session=self.session,
                model=model,
                thinking_level=self.selected.owner.thread.thinking_level,
                environment=ProjectRuntimeRequest.native_environment(
                    self.selected.owner.thread,
                    self.selected.bus.bus.path.parent, registry,
                ),
                ingress=MaintenanceBarrier(self.selected.bus._registry.store.path),
                startup=startup,
                selected_tool_mode=selected_tool_mode,
                observe_event=observe_event,
                request_observer=partial(
                    record_request_progress, self.selected.bus.bus.path.parent, lease,
                ),
                prompt_send_boundary=self,
                execution=execution,
                following=following,
            )
        except NativePiTerminalFailure as error:
            def settle(store: Coordination, terminal: NativePiTerminalFailure[NativeContextProof] = error) -> object:
                self.verify(store, terminal.context, source_reads)
                return self.stage.fail_terminal(
                    store, self.selected.identity, self.input_id, self.token_digest,
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
                record_acquisition_progress, self.selected.bus.bus.path.parent, lease,
                self.input_id, self.measurements, startup.native_process,
            ))
        async with self.selected.cursor_delivery.reading() as source_reads:
            await Coordination.run_read_async(
                self.store_path, lambda store: self.verify(store, result.context, source_reads),
            )
        return result

    async def context_committed(
        self, context: NativeContextProof, source_reads: NativeEvidenceScope,
    ) -> None:
        """Publish corroborated entry while the model is still running.

        The context journal is borrowed before coordinator locking, as on the
        terminal path. Neither the RPC event nor model activity proves context inclusion.
        """
        def acknowledge(store: Coordination) -> None:
            self.verify(store, context, source_reads)
            self.stage.acknowledge_context(
                store, self.selected.identity, self.input_id, self.token_digest, context,
            )
        await Coordination.run_async(self.store_path, acknowledge)

    def commit(self, store: Coordination, context: NativeContextProof) -> None:
        self.stage.acknowledge_context(
            store, self.selected.identity, self.input_id, self.token_digest, context,
        )

    def _start_once(self, identity: NativeSessionIdentity) -> None:
        """Commit the exact grant under original fences, then release them.

        Stop, rename and maintenance before this grant refuse the input. After
        it, they are ordered after the permitted attempt, whose raw
        writer retains its own pipe and token. The recorded send grant remains
        the original recovery evidence; neither successful pipe completion nor
        subsequent owner loss permits replay.
        """
        with ExitStack() as authority:
            with self.measurements.operation("coordinator_open"):
                store = authority.enter_context(Coordination(str(self.store_path)))
            with self.measurements.operation("maintenance_ingress"):
                authority.enter_context(MaintenanceBarrier(
                    self.selected.bus._registry.store.path
                ).allow_input())
            with self.measurements.operation("registry_exclusion"):
                cut = authority.enter_context(self.selected.bus._registry.store.current_snapshot())
                registry = cut.snapshot
            with self.measurements.operation("prompt_binding"):
                authority.enter_context(self.stage.bound_prompt(
                    store, self.input_id, self.selected.identity, self.wire_root_id,
                    self.prompt.text,
                ))
            with self.measurements.operation("coordinator_exclusive"):
                db = authority.enter_context(store.session.writing())
            with self.measurements.operation("selected_source_check"):
                self.session.verify_start(identity)
            # From token consumption through COMMIT/fsync, every failure
            # remains uncertain and propagates without another send attempt.
            if not self._once.acquire(blocking=False):
                raise IdentityConflict("native send claim_scope cannot be reused")
            with self.measurements.operation("claim_checks"):
                self.selected.owner.require_snapshot(
                    registry, "recipient registry owner changed before native send"
                )
                NativeRuntimeTable.require_installed(db)
                self.selected.identity.require(store, self.stage.recipient_lookup)
                reserved = self.stage.require_reservation(
                    db, self.input_id, self.selected.identity, self.token_digest
                )
                self.stage.require_claim(store)
            with self.measurements.operation("native_claim_record"):
                reserved.sent_owner_claimed_generation.record(
                    reserved, db, self.selected.owner.claimed_generation, identity
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
    def prompt_boundary(self, turn: TrackedTurnSession) -> Iterator[None]:
        with self(turn.native.attestation.identity.require_identity()):
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
