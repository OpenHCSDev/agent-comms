"""Session input ownership: acceptance, delivery cursors, queues and wake draining."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from contextlib import aclosing
from typing import Any

from acp import RequestError
from acp.schema import (
    AgentMessageChunk,
    PromptResponse,
    TextContentBlock,
)

from .acp_extension import (
    AvailableQueueProjection,
    PromptRequest,
    InputDeliveryChangedUpdate,
    InputStartedUpdate,
    QueueChangedUpdate,
    QueueItem,
    QueueScope,
    UnavailableQueueProjection,
    encode_updates,
)
from .activity import StoppedDrainDiagnostic, UnavailableDrainDiagnostic
from .agent_events import Done
from .comms import Comms
from .input_origin import InputOrigin, UnattributedInputOrigin
from .coordination_errors import CoordinationError
from .input_attempt import InputAttempt
from .input_disposition import FutureInputQueue, InputDispositions
from .input_effects import InputEffects
from .queued_input import InitialInput, QueuedInput
from .routing import ScheduledTurn
from .runtime import UNBOUND_CONTROLLER, RuntimeServer
from .schedule_rules import WakeScheduleCheck
from .selected_summary_admission import SelectedSummaryAdmission
from .session_lifecycle import SessionLifecycle
from .store_files import _store_lock, file_revision
from .thread_identity import AdmissionIdentity
from .threads import Thread
from .turn_input_source import OriginalTurnInput, AcceptedFollowingInput
from .wire_watch import WireWatch

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
        self.backend_inboxes: dict[str, asyncio.Queue[str | dict[str, Any]]] = {}
        self.queued_inputs: dict[str, dict[str, QueuedInput]] = {}
        self.restored_inputs: dict[str, dict[str, QueuedInput]] = {}
        self.queue_revisions: dict[str, int] = {}
        self.following_sources: dict[str, dict[str, AcceptedFollowingInput]] = {}
        self.turn_input_keys: dict[str, set[str]] = {}
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
        with _store_lock(self.comms._wire_lock_path):
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

    def queue_binding(self, session_id: str) -> QueueScope | None:
        try:
            owner, admission = self.comms.registry.live_owner_with_admission(
                self.sessions.bindings.get(session_id, session_id)
            )
        except (OSError, ValueError):
            return None
        return QueueScope(
            session_id,
            AdmissionIdentity(owner.incarnation, admission),
            owner.pid,
        )

    def queue_state(self, session_id: str) -> QueueChangedUpdate:
        """The producer owns the complete exact-ID queue projection."""
        revision = self.queue_revisions.get(session_id, 0) + 1
        self.queue_revisions[session_id] = revision
        scope = self.queue_binding(session_id)
        if scope is None:
            return QueueChangedUpdate(None, revision, UnavailableQueueProjection())

        def current(values: dict[str, QueuedInput]) -> tuple[QueueItem, ...]:
            return tuple(
                QueueItem(input_id, item.text)
                for input_id, item in values.items()
                if item.context.owns(scope.admission)
            )

        items = current(self.queued_inputs.get(session_id, {}))
        restored = current(self.restored_inputs.get(session_id, {}))
        rows = items + restored
        try:
            sizes = [len(row.text.encode("utf-8")) for row in rows]
        except (AttributeError, UnicodeError):
            return QueueChangedUpdate(scope, revision, UnavailableQueueProjection())
        if len(rows) > 32 or any(size > 4096 for size in sizes) or sum(sizes) > 65536:
            return QueueChangedUpdate(scope, revision, UnavailableQueueProjection())
        return QueueChangedUpdate(scope, revision, AvailableQueueProjection(items, restored))

    async def emit_queue_state(self, session_id: str, *, client: Any = None) -> None:
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(self.queue_state(session_id)),
            ),
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
        scope = self.queue_binding(session_id)
        if source_scope is None or scope is None or not source_scope.relation(scope).current:
            scope = None
        revision = None
        if scope is not None:
            revision = self.queue_revisions.get(session_id, 0) + 1
            self.queue_revisions[session_id] = revision
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(InputStartedUpdate(input_id, text, scope, revision, native_id)),
            ),
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
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(InputDeliveryChangedUpdate(input_id)),
            ),
        )

    async def replay_unknown_inputs(self, session_id: str, client: Any = None) -> None:
        """Refresh the producer-owned delivery ledger; this never replays an input."""
        await self.emit_input_delivery_changed(session_id, client=client)

    def awaiting_input_keys(self, session_id: str) -> frozenset[str] | None:
        """Derive delivery notices from existing owner queues; never create new authority."""
        owner = self.comms.registry.require(self.sessions.require(session_id))
        if owner.pid != os.getpid() or not self.comms.registry.status(owner.name).running:
            return None
        keys = set(self.turn_input_keys.get(session_id, ()))
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
                owner = self.comms.registry.snapshot().owner_identity(thread)
                try:
                    await self.drain_inbox(session_id)
                    await self.sessions.config.sync_thread(session_id)
                    if time.monotonic() >= next_goal_wait_check:
                        self.comms.goals.recover_closed_goal_wait(session_id)
                        next_goal_wait_check = time.monotonic() + GOAL_WAIT_RECHECK_INTERVAL
                    self.effects.turns.goals.schedule_goal(session_id)
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
                    self.comms.agents.set_drain_diagnostic(
                        thread,
                        owner,
                        UnavailableDrainDiagnostic(owner, type(error).__name__, str(error)),
                    )
                except Exception as error:
                    self.comms.agents.set_drain_diagnostic(
                        thread,
                        owner,
                        StoppedDrainDiagnostic(owner, type(error).__name__, str(error)),
                    )
                    raise
                else:
                    if (
                        not self.comms.registry.require(thread).executing
                        and session_id not in self.effects.turns.turn_tasks
                        and session_id not in self.backend_inboxes
                    ):
                        self.comms.agents.set_drain_diagnostic(thread, owner, None)

    async def drain_inbox(self, session_id: str) -> int:
        """Push undelivered messages to the client; returns count pushed."""
        async with self.drain_locks.setdefault(session_id, asyncio.Lock()):
            return await self.drain_owned_inbox(session_id)

    def _private_observation_revision(self, session_id: str, root_id: str) -> tuple:
        """Cheap source revisions; never a receipt, cursor, or owner authority."""
        return (
            root_id,
            self.effects._private_nk_native_package,
            self.auto_wake,
            self.sessions.runtime_enabled,
            self.sessions.bindings.get(session_id),
            session_id in self.backend_inboxes,
            self.effects.turns.turn_state(session_id).busy,
            session_id in self.effects.turns.turn_tasks,
            file_revision(self.comms.bus.log.path),
            file_revision(self.comms.registry.store.path),
            file_revision(self.comms.root / ".registry-owner-guard"),
            file_revision(self.comms.root / "coordination.sqlite3"),
        )

    async def _drain_private_if_changed(self, session_id: str, root_id: str) -> int:
        # The caller still validates the current private marker each time. The
        # ordinary loop still synchronizes configuration and schedules goals.
        before = self._private_observation_revision(session_id, root_id)
        if self._idle_private_revisions.get(session_id) == before:
            await self.effects.cursors.refresh(session_id, self.sessions.require(session_id))
            return 0
        self._idle_private_revisions.pop(session_id, None)
        result = await self.effects._drain_private_nk(session_id, root_id)
        # Never absorb a message/owner/recovery change during a suspended read,
        # or skip queued independent work after a completed native turn.
        if result == 0 and before == self._private_observation_revision(session_id, root_id):
            self._idle_private_revisions[session_id] = before
        return result

    async def drain_owned_inbox(self, session_id: str) -> int:
        private_root = self.effects._private_nk_marker()
        pushed = await self._drain_private_if_changed(session_id, private_root)
        WakeScheduleCheck(session_id=session_id, inputs=self).schedule()
        return pushed

    @property
    def background_wakes_disabled(self) -> bool:
        return not self.auto_wake or not self.sessions.runtime_enabled

    async def drain_count(self, session_id: str) -> int:
        return await self.drain_inbox(session_id)

    def pending_followups(self, session_id: str) -> int:
        rows = self.dispositions.read()
        return sum(
            not rows.all_started(source.keys)
            for source in self.following_sources.get(session_id, {}).values()
        )

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
        with _store_lock(self.comms._wire_lock_path):
            if self.pending_followups(session_id) >= 32:
                raise RequestError.invalid_params(
                    {"reason": "Too many follow-up inputs awaiting their own user start."}
                )
            controller = self.runtime.controller.get()
            if controller is UNBOUND_CONTROLLER:
                controller = self.sessions.client
            item, owner = QueuedInput.capture(
                self,
                self.sessions.require(session_id),
                text=display_text,
                prompt="User follow-up:\n" + text.removeprefix(AGENT_PREFIX),
                echo=request.defer_display,
                images=images,
                controller=controller,
                input_id=request.input_id,
                origin=request.origin,
            )
            item = request.accepted(item, self.dispositions.read().lookup(item.key), owner)
            self.following_sources.setdefault(session_id, {})[item.input_id] = item.source()
            self.turn_input_keys.setdefault(session_id, set()).add(item.key)
            self.queued_inputs.setdefault(session_id, {})[item.input_id] = item
            inbox.put_nowait(
                {
                    "type": "prompt",
                    "message": item.prompt,
                    "streamingBehavior": "steer",
                    "_input_id": item.input_id,
                    **({"images": [image.to_rpc() for image in images]} if images else {}),
                }
            )
            request.enqueue_control(inbox, item.input_id)
        await request.publish_acceptance(self, session_id)
        return PromptResponse(
            stop_reason="end_turn",
            field_meta=encode_updates(InputDeliveryChangedUpdate(item.input_id)),
        )

    def bind_native_turn(
        self, session_id: str, owner: Thread, admission: int, turn_id: str
    ) -> asyncio.Queue[str | dict[str, Any]]:
        """Transfer existing live inputs to the new lease, never read them from disk."""
        inbox = self.backend_inboxes.setdefault(session_id, asyncio.Queue())
        for input_id, item in self.queued_inputs.get(session_id, {}).items():
            self.queued_inputs[session_id][input_id] = item.bind_turn(
                owner, admission, turn_id
            )
        return inbox

    def future_inputs(
        self, owner: Thread, pending_input_key: str | None
    ) -> dict[str, InputAttempt]:
        """Live queued receipts only; durable UNKNOWN alone never grants this exception.

        The bridge holds the wire lock while reading this owner. Acceptance,
        clear and promotion use that same lock, including their in-memory edits.
        No receipt survives a process restart or an owner/turn change.
        """
        if owner.pid != os.getpid() or owner.active_turn is None or self.closing:
            return {}
        result = {}
        for session_id, original in self.original_sources.items():
            if (
                original.notice_keys != (pending_input_key,)
                or self.sessions.bindings.get(session_id) != owner.name
            ):
                continue
            for input_id, item in self.queued_inputs.get(session_id, {}).items():
                receipt = item.future_receipt(owner)
                if receipt is not None:
                    result[item.key] = receipt
        return result

    def send_now(self, session_id: str) -> None:
        with _store_lock(self.comms._wire_lock_path):
            inbox = self.backend_inboxes.get(session_id)
            queued = self.queued_inputs.get(session_id, {})
            if inbox is not None and queued:
                for key, item in tuple(queued.items()):
                    queued[key] = item.immediate()
                inbox.put_nowait({"type": "interrupt_steering", "_input_ids": list(queued)})

    async def stop_wakes(self) -> None:
        self.closing = True
        for task in self.wake_tasks.values():
            task.cancel()
        await asyncio.gather(*self.wake_tasks.values(), return_exceptions=True)
        self.wake_tasks.clear()

    async def close(self) -> None:
        tasks = tuple(self.drain_tasks.values())
        self.drain_tasks.clear()
        self._idle_private_revisions.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
        for key in started_keys:
            row = self.dispositions.read().rows.get(key)
            if row is not None and not row.unresolved:
                await self.emit_input_disposition(session_id, row)
        row = self.dispositions.read().lookup(started_keys[0] if len(started_keys) == 1 else None)
        if input_id is None and original.notice_keys:
            row = self.dispositions.read().lookup(original.notice_keys[0])
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
            self.turn_input_keys.get(session_id, set()).difference_update(source.keys)
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

    async def finish_turn_inputs(
        self, session_id: str, inbox: asyncio.Queue[str | dict[str, Any]]
    ) -> None:
        """Retire this turn's live capabilities; retain UNKNOWN only as notices."""
        if self.backend_inboxes.get(session_id) is inbox:
            self.backend_inboxes.pop(session_id, None)
        while not inbox.empty():
            pending = inbox.get_nowait()
            if isinstance(pending, str):
                self.pending_turns.setdefault(session_id, []).append(ScheduledTurn(pending))
        with _store_lock(self.comms._wire_lock_path):
            original = self.original_sources.pop(session_id, None)
            self.dispositions.settle_unbound(original.notice_keys if original else ())
        self.following_sources.pop(session_id, None)
        self.turn_input_keys.pop(session_id, None)
        admission = self.selected_summary_admissions.pop(session_id, None)
        if admission is not None:
            admission.invalidate()
        await self.emit_input_delivery_changed(session_id)
        remaining = self.queued_inputs.pop(session_id, {})
        if remaining:
            self.restored_inputs.setdefault(session_id, {}).update(
                {
                    key: restored
                    for key, item in remaining.items()
                    if (restored := item.restore_after_turn()) is not None
                }
            )
            await self.emit_queue_state(session_id)

    async def run_owned_input(
        self,
        session_id: str,
        thread_name: str,
        task: str,
        *,
        images: tuple[Any, ...] = (),
        display_text: str | None = None,
        input_id: str | None = None,
        origin: InputOrigin = UnattributedInputOrigin(),
    ) -> None:
        with _store_lock(self.comms._wire_lock_path):
            item, _owner = InitialInput.capture(
                self,
                thread_name,
                text=display_text or task,
                prompt=task,
                echo=display_text is not None,
                images=images,
                controller=self.runtime.controller.get(),
                input_id=input_id,
                origin=origin,
            )
            self.queued_inputs.setdefault(session_id, {})[item.input_id] = item
        try:
            await self.emit_input_disposition(session_id, self.dispositions.read().lookup(item.key))
            await self.emit_queue_state(session_id)
            await item.dispatch(self.effects.turns, session_id, thread_name)
        finally:
            queued = self.queued_inputs.get(session_id, {})
            if queued.get(item.input_id) is item:
                queued.pop(item.input_id)
                await self.emit_queue_state(session_id)
