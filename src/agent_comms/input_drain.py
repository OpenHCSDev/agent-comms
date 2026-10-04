"""Session input ownership: acceptance, delivery cursors, queues and wake draining."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import replace
from contextlib import aclosing
from functools import partial
from typing import Any

from acp import RequestError
from acp.schema import PromptResponse

from .acp_extension import (
    PromptRequest,
    InputDeliveryChangedUpdate,
    InputStartedUpdate,
    QueueChangedUpdate,
    QueueScope,
    QueueProjection,
    encode_updates,
)
from .activity import StoppedDrainDiagnostic, UnavailableDrainDiagnostic
from .agent_events import Done
from .comms import Comms
from .coordination_errors import CoordinationError, IdentityConflict
from .coordination_cohort import next_sealed_assignment
from .coordinator import Coordination
from .bus_publication import stable_thread_lookup
from .cursor_owner import CursorOwner
from .child_process import join_retirement
from .native_input_owner import RegistryOwner
from .input_attempt import InputAttempt
from .input_disposition import FutureInputQueue, InputDispositions, InputDocument
from .input_effects import InputEffects
from .queued_input import QueuedInput, InputHandoffRefused
from .routing import ScheduledTurn
from .runtime import UNBOUND_CONTROLLER, RuntimeServer
from .schedule_rules import WakeScheduleCheck
from .selected_summary_admission import SelectedSummaryAdmission
from .session_lifecycle import SessionLifecycle
from .store_files import _async_store_lock, _store_lock
from .thread_identity import AdmissionIdentity
from .threads import Thread
from .turn_input_source import OriginalTurnInput, AcceptedFollowingInput
from .wire_watch import WireWatch
from .field_codec import FieldCodec
from .turn_context import TurnContext, UserFollowupSegment

AGENT_PREFIX = "!agent "
GOAL_WAIT_RECHECK_INTERVAL = 60.0


class InputDrain(FutureInputQueue):
    def __init__(
        self,
        comms: Comms,
        sessions: SessionLifecycle,
        runtime: RuntimeServer,
        effects: InputEffects,
        auto_wake: bool,
    ):
        self.comms = comms
        self.sessions = sessions
        self.runtime = runtime
        self.effects = effects
        self.drain_tasks: dict[str, asyncio.Task[None]] = {}
        self.backend_inboxes: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.queued_inputs: dict[str, dict[str, QueuedInput]] = {}
        self.restored_inputs: dict[str, dict[str, QueuedInput]] = {}
        self.queue_revisions: dict[str, int] = {}
        self.following_sources: dict[str, dict[str, AcceptedFollowingInput]] = {}
        self.original_sources: dict[str, OriginalTurnInput] = {}
        self.selected_summary_admissions: dict[str, SelectedSummaryAdmission] = {}
        self.dispositions = InputDispositions(comms.root / InputDispositions.filename)
        self.auto_wake = auto_wake
        self.pending_turns: dict[str, list[ScheduledTurn]] = {}
        self.drain_locks: dict[str, asyncio.Lock] = {}
        self.closing = False
        self.wake_tasks: dict[str, asyncio.Task[None]] = {}
        # Only a completed, quiescent observation can suppress another scan.
        # These revisions never authorize delivery, acceptance or a native send.
        self._idle_private_revisions: dict[str, tuple] = {}

    async def clear_queued_inputs(self, session_id: str) -> None:
        """Drop prompts queued against the backend for this session.

        Pi's own queue is cleared, then the local deferred-display list is
        reset. Callers may re-send the prompts they want to keep.
        """
        async with _async_store_lock(self.comms._wire_lock_path):
            inbox = self.backend_inboxes.get(session_id)
            if inbox is not None:
                inbox.put_nowait({"type": "clear_queue"})
            self.queued_inputs[session_id] = {
                key: retained
                for key, item in self.queued_inputs.get(session_id, {}).items()
                if (retained := item.after_clear()) is not None
            }
            self.restored_inputs.pop(session_id, None)
        await self.emit_queue_state(session_id)

    async def queue_binding(self, session_id: str) -> QueueScope | None:
        try:
            owner, admission = await Coordination.run_worker(partial(
                self.comms.registry.live_owner_with_admission,
                self.sessions.bindings.get(session_id, session_id),
            ))
        except (OSError, ValueError):
            return None
        return QueueScope(
            session_id,
            AdmissionIdentity(owner.incarnation, admission),
            owner.pid,
        )

    async def queue_state(self, session_id: str) -> QueueChangedUpdate:
        """The producer owns the complete exact-ID queue projection."""
        scope = await self.queue_binding(session_id)
        revision = self.queue_revisions.get(session_id, 0) + 1
        self.queue_revisions[session_id] = revision
        return QueueChangedUpdate(scope, revision, QueueProjection.capture(
            scope,
            tuple(self.queued_inputs.get(session_id, {}).values()),
            tuple(self.restored_inputs.get(session_id, {}).values()),
        ))

    async def emit_queue_state(self, session_id: str, *, client: Any = None) -> None:
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=(await self.queue_state(session_id)).acp_chunk(),
        )

    async def emit_input_started(
        self,
        session_id: str,
        text: str | None,
        input_id: str | None = None,
        source_scope: QueueScope | None = None,
        *,
        native_id: str | None,
        client: Any = None,
    ) -> None:
        scope = await self.queue_binding(session_id)
        if source_scope is None or scope is None or not source_scope.relation(scope).current:
            scope = None
        revision = None
        if scope is not None:
            revision = self.queue_revisions.get(session_id, 0) + 1
            self.queue_revisions[session_id] = revision
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=InputStartedUpdate(input_id, text, scope, revision, native_id).acp_chunk(),
        )

    async def emit_input_disposition(
        self, session_id: str, row: InputAttempt, client: Any = None
    ) -> None:
        await self.emit_input_delivery_changed(session_id, input_id=row.public_id, client=client)

    async def emit_input_delivery_changed(
        self, session_id: str, *, input_id: str | None = None, client: Any = None
    ) -> None:
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=InputDeliveryChangedUpdate(input_id).acp_chunk(),
        )

    async def replay_unknown_inputs(self, session_id: str, client: Any = None) -> None:
        """Refresh the producer-owned delivery ledger; this never replays an input."""
        await self.emit_input_delivery_changed(session_id, client=client)

    def input_keys(self, session_id: str) -> frozenset[str]:
        """Live captured source membership, never rebuilt from durable notices."""
        original = self.original_sources.get(session_id)
        following = frozenset(
            key for source in self.following_sources.get(session_id, {}).values()
            for key in source.keys
        )
        return following.union(original.keys) if original is not None else following

    async def awaiting_input_keys(self, session_id: str) -> frozenset[str] | None:
        """Derive delivery notices from existing owner queues; never create new authority."""
        snapshot = await Coordination.run_worker(self.comms.registry.snapshot)
        owner = snapshot.require(self.sessions.require(session_id))
        if owner.pid != os.getpid() or not snapshot.status(owner.name).running:
            return None
        keys = set(self.input_keys(session_id))
        keys.update(
            self.dispositions.bus_key(turn.origin, owner)
            for turn in self.pending_turns.get(session_id, ())
            if turn.origin is not None
        )
        return frozenset(keys)

    def ensure_live_drain(self, session_id: str) -> None:
        """Keep one background task per session pushing inbox messages live."""
        if self.closing:
            return
        if session_id in self.drain_tasks and not self.drain_tasks[session_id].done():
            return

        self.drain_tasks[session_id] = self.runtime.background(self.observe(session_id))

    async def observe(self, session_id: str) -> None:
        next_goal_wait_check = 0.0
        async with aclosing(WireWatch.observations(self.comms.root)) as observations:
            async for _ in observations:
                thread = self.sessions.require(session_id)
                snapshot = await Coordination.run_worker(self.comms.registry.snapshot)
                owner = snapshot.owner_identity(thread)
                try:
                    await self.drain_inbox(session_id)
                    await self.sessions.config.sync_thread(session_id)
                    if time.monotonic() >= next_goal_wait_check:
                        await Coordination.run_worker(partial(
                            self.comms.goals.recover_closed_goal_wait, session_id,
                        ))
                        next_goal_wait_check = time.monotonic() + GOAL_WAIT_RECHECK_INTERVAL
                    await self.effects.turns.goals.schedule_goal(session_id)
                    await self.sessions.config.refresh_auth_models()
                except asyncio.CancelledError:
                    raise
                except (
                    OSError,
                    ValueError,
                    sqlite3.Error,
                    CoordinationError,
                    RequestError,
                ) as error:
                    await Coordination.run_worker(partial(
                        self.comms.agents.set_drain_diagnostic, thread, owner,
                        UnavailableDrainDiagnostic(owner, type(error).__name__, str(error)),
                    ))
                except Exception as error:
                    await Coordination.run_worker(partial(
                        self.comms.agents.set_drain_diagnostic, thread, owner,
                        StoppedDrainDiagnostic(owner, type(error).__name__, str(error)),
                    ))
                    raise
                else:
                    current = await Coordination.run_worker(partial(self.comms.registry.require, thread))
                    if (
                        not current.executing
                        and session_id not in self.effects.turns.turn_tasks
                        and session_id not in self.backend_inboxes
                    ):
                        await Coordination.run_worker(partial(
                            self.comms.agents.set_drain_diagnostic, thread, owner, None,
                        ))

    async def drain_inbox(self, session_id: str) -> int:
        """Push undelivered messages to the client; returns count pushed."""
        async with self.drain_locks.setdefault(session_id, asyncio.Lock()):
            return await self.drain_owned_inbox(session_id)

    def _private_observation_revision(
        self, session_id: str, root_id: str, wire: tuple, store: Coordination,
    ) -> tuple:
        """Original owner/work observations, never delivery or replay authority.

        Other owners' registry phases and SQL writes cannot change this owner's
        observation. Committed wire messages, own pending/recovered work and
        physical replacement still can. Silent context records consume bytes,
        not the message cut used to select delivery.
        """
        registry = self.comms.registry.snapshot()
        thread = registry.require(self.sessions.require(session_id))
        owner = RegistryOwner(
            thread=thread, admission_generation=registry.admission_generations[thread.name],
        )
        with store.session.read():
            participant = store.participants.get(stable_thread_lookup(owner.thread.created_at))
            pending = next_sealed_assignment(
                store, participant.lookup, owner.thread.name, after_seq=wire[-1],
            )
            cursor = CursorOwner(
                thread=owner.thread, admission_generation=owner.admission_generation,
                wire_root_id=root_id, generation=participant.participant_generation,
            ).cursor(store.session._connection)
            original = store.session.path.stat()
        return (
            root_id,
            self.effects._private_nk_native_package,
            self.auto_wake,
            self.sessions.runtime_enabled,
            self.sessions.bindings.get(session_id),
            session_id in self.backend_inboxes,
            self.effects.turns.turn_state(session_id).busy,
            session_id in self.effects.turns.turn_tasks,
            wire,
            owner,
            registry.statuses[thread.name],
            participant,
            pending,
            cursor,
            (original.st_dev, original.st_ino),
        )

    async def _observe_private_revision(self, session_id: str, root_id: str) -> tuple:
        # Pending acquisition is not an inbox failure or a completed read.
        # Cancellation retires only this observation's waiting descriptor.
        def wire_revision(source):
            if source.marker.root_id != root_id:
                raise IdentityConflict("private drain original root changed")
            return (
                source.witness.source_identity,
                source.committed_sequence(),
                source.marker.admission_after_seq,
            )
        wire = await self.comms.bus.log.read_certified_async(wire_revision)
        # No physical bus custody survives into registry/SQL consumer work.
        # The original coordinator owns its bounded read acquisition policy;
        # the observation must not replace it with a zero-wait failure.
        return await Coordination.run_async(
            self.comms.root / "coordination.sqlite3",
            partial(self._private_observation_revision, session_id, root_id, wire),
        )

    async def _drain_private_if_changed(self, session_id: str, root_id: str) -> int:
        # The caller still validates the current private marker each time. The
        # ordinary loop still synchronizes configuration and schedules goals.
        before = await self._observe_private_revision(session_id, root_id)
        if self._idle_private_revisions.get(session_id) == before:
            await self.effects.cursors.refresh(session_id, self.sessions.require(session_id))
            return 0
        self._idle_private_revisions.pop(session_id, None)
        result = await self.effects._drain_private_nk(session_id, root_id)
        # Never absorb a message/owner/recovery change during a suspended read,
        # or skip queued independent work after a completed native turn.
        if result == 0 and before == await self._observe_private_revision(session_id, root_id):
            self._idle_private_revisions[session_id] = before
        return result

    async def drain_owned_inbox(self, session_id: str) -> int:
        private_root = await Coordination.run_worker(self.effects._private_nk_marker)
        pushed = await self._drain_private_if_changed(session_id, private_root)
        WakeScheduleCheck(session_id=session_id, inputs=self).schedule()
        return pushed

    @property
    def background_wakes_disabled(self) -> bool:
        return not self.auto_wake or not self.sessions.runtime_enabled

    async def drain_count(self, session_id: str) -> int:
        return await self.drain_inbox(session_id)

    async def accept_followup(
        self,
        session_id: str,
        *,
        text: str,
        display_text: str,
        request: PromptRequest,
        images: tuple[Any, ...],
    ) -> PromptResponse:
        inbox = self.backend_inboxes[session_id]
        controller = self.runtime.controller.get()
        if controller is UNBOUND_CONTROLLER:
            controller = self.sessions.client
        followup = UserFollowupSegment.capture(text.removeprefix(AGENT_PREFIX))
        async with QueuedInput.reserve(
            self, session_id, self.sessions.require(session_id), text=display_text,
            prompt=TurnContext.render_segments((followup,), images=images).text,
            echo=request.defer_display, images=images, controller=controller,
            input_id=request.input_id, origin=request.origin,
        ) as (item, owner, source, custody):
            if self.backend_inboxes.get(session_id) is not inbox:
                raise InputHandoffRefused("Original native inbox retired before follow-up handoff")
            item = request.accepted(item, source, owner)
            followup = replace(followup, provenance=(*followup.provenance, source.context_provenance()))
            rendered = TurnContext.render_segments((followup,), images=images)
            self.following_sources.setdefault(session_id, {})[item.input_id] = item.source()
            self.queued_inputs.setdefault(session_id, {})[item.input_id] = item
            inbox.put_nowait(
                {
                    "type": "prompt",
                    "message": item.prompt,
                    "contextContributions": FieldCodec.encode(rendered.contributions),
                    "streamingBehavior": "steer",
                    "_input_id": item.input_id,
                    **({"images": [image.to_rpc() for image in images]} if images else {}),
                }
            )
            request.enqueue_control(inbox, item.input_id)
            custody.pop_all()
        await request.publish_acceptance(self, session_id)
        return PromptResponse(
            stop_reason="end_turn",
            field_meta=encode_updates(InputDeliveryChangedUpdate(item.input_id)),
        )

    def bind_native_turn(
        self, session_id: str, owner: Thread, admission: int, turn_id: str
    ) -> asyncio.Queue[dict[str, Any]]:
        """Transfer existing live inputs to the new lease, never read them from disk."""
        inbox = self.backend_inboxes.setdefault(session_id, asyncio.Queue())
        for input_id, item in self.queued_inputs.get(session_id, {}).items():
            self.queued_inputs[session_id][input_id] = item.bind_turn(
                owner, admission, turn_id
            )
        return inbox

    def future_inputs(
        self, owner: Thread, pending_input_keys: tuple[str, ...]
    ) -> dict[str, InputAttempt]:
        """Live queued receipts only; durable UNKNOWN alone never grants this exception.

        The bridge holds the wire lock while reading this owner. Acceptance,
        clear and promotion use that same lock, including their in-memory edits.
        No receipt survives a process restart or an owner/turn change.
        """
        if not pending_input_keys:
            return {}
        if owner.pid != os.getpid() or owner.active_turn is None or self.closing:
            return {}
        result = {}
        for session_id, original in self.original_sources.items():
            if (
                original.compaction_keys(owner.session_file) != pending_input_keys
                or self.sessions.bindings.get(session_id) != owner.name
            ):
                continue
            for input_id, item in self.queued_inputs.get(session_id, {}).items():
                receipt = item.future_receipt(owner)
                if receipt is not None:
                    result[item.key] = receipt
        return result

    async def send_now(self, session_id: str) -> None:
        async with _async_store_lock(self.comms._wire_lock_path):
            inbox = self.backend_inboxes.get(session_id)
            queued = self.queued_inputs.get(session_id, {})
            if inbox is not None and queued:
                for key, item in tuple(queued.items()):
                    queued[key] = item.immediate()
                inbox.put_nowait({"type": "interrupt_steering", "_input_ids": list(queued)})

    async def close(self) -> None:
        """Retire input producers before their sessions and turns are closed."""
        self.closing = True
        tasks = (*self.drain_tasks.values(), *self.wake_tasks.values())
        for task in tasks:
            task.cancel()
        try:
            await join_retirement(asyncio.gather(*tasks, return_exceptions=True))
        finally:
            self.drain_tasks.clear()
            self.wake_tasks.clear()
            self._idle_private_revisions.clear()

    async def input_started(
        self,
        session_id: str,
        input_id: str | None,
        source_scope: QueueScope,
    ) -> None:
        original = self.original_sources[session_id]
        input_id = input_id if input_id is not None else original.accepted_id
        source = self.following_sources.get(session_id, {}).get(input_id)
        started_keys = original.keys if input_id is None else source.keys if source else ()
        document = await Coordination.run_worker(self.dispositions.read)
        for key in started_keys:
            row = document.rows.get(key)
            if row is not None and not row.unresolved:
                await self.emit_input_disposition(session_id, row)
        row = document.lookup(started_keys[0] if len(started_keys) == 1 else None)
        if input_id is None and original.notice_keys:
            row = document.lookup(original.notice_keys[0])
        row = row.require_started(source_scope.admission_generation)
        if input_id is None and original.notice_keys:
            input_id = row.public_id
        item = self.queued_inputs.get(session_id, {}).get(input_id or "")
        text = item.text if item else original.notice_text if input_id == row.public_id else None
        await self.emit_input_started(
            session_id,
            text,
            input_id,
            source_scope=source_scope,
            native_id=row.native_id,
        )
        self.queued_inputs.get(session_id, {}).pop(input_id or "", None)
        await self.emit_queue_state(session_id)

    async def input_refused(self, session_id: str, input_id: str | None) -> None:
        if input_id is None:
            return
        source = self.following_sources.get(session_id, {}).pop(input_id, None)
        if source is not None:
            # Native write was refused; keep the durable notice, never replay it.
            await self.emit_input_delivery_changed(session_id)
        if self.queued_inputs.get(session_id, {}).pop(input_id, None):
            await self.emit_queue_state(session_id)

    def continue_in_project(
        self, session_id: str, terminal: Done, original_project: str, current_project: str
    ) -> None:
        if self.closing or current_project == original_project:
            return
        if continuation := terminal.project_continuation(current_project):
            self.pending_turns.setdefault(session_id, []).append(continuation)

    def finish_original_inputs(self, keys: tuple[str, ...]) -> InputDocument:
        """The joined retirement owns the original input and its wire cut."""
        with _store_lock(self.comms._wire_lock_path):
            return self.dispositions.settle_unbound(keys)

    async def finish_turn_inputs(
        self, session_id: str, inbox: asyncio.Queue[dict[str, Any]]
    ) -> None:
        """Retire this turn's live capabilities; retain UNKNOWN only as notices."""
        if self.backend_inboxes.get(session_id) is inbox:
            self.backend_inboxes.pop(session_id, None)
        while not inbox.empty():
            inbox.get_nowait()
        try:
            original = self.original_sources.pop(session_id, None)
            if original is not None:
                await Coordination.run_worker(partial(self.finish_original_inputs, original.keys))
        finally:
            # Cancellation joins the original input write before retiring loop
            # capabilities. No callback delivery is required to burn a grant or
            # retain accepted queued input for the next distinct turn.
            self.following_sources.pop(session_id, None)
            admission = self.selected_summary_admissions.pop(session_id, None)
            if admission is not None:
                admission.invalidate()
            remaining = self.queued_inputs.pop(session_id, {})
            if remaining:
                self.restored_inputs.setdefault(session_id, {}).update(
                    {
                        key: restored
                        for key, item in remaining.items()
                        if (restored := item.restore_after_turn()) is not None
                    }
                )
        await self.emit_input_delivery_changed(session_id)
        if remaining:
            await self.emit_queue_state(session_id)
