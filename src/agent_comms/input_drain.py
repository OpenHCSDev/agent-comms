"""Session input ownership: acceptance, delivery cursors, queues and wake draining."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from acp import RequestError
from acp.schema import (
    AgentMessageChunk,
    PromptResponse,
    TextContentBlock,
)

from .acp_extension import (
    AvailableQueueProjection,
    InputDeliveryChangedUpdate,
    InputStartedUpdate,
    QueueChangedUpdate,
    QueueItem,
    QueueScope,
    UnavailableQueueProjection,
    encode_updates,
)
from .comms import Comms
from .goal_waits import GoalWait
from .goals import Goal
from .image_inputs import ImageInput
from .input_attempt import InputAttempt
from .input_disposition import FutureInputQueue, InputDispositions
from .input_effects import InputEffects
from .routing import ScheduledTurn
from .runtime import UNBOUND_CONTROLLER, RuntimeServer
from .selected_summary_admission import SelectedSummaryAdmission
from .session_lifecycle import SessionLifecycle
from .store_files import _store_lock, file_revision
from .thread_identity import OwnerIdentity, ThreadIncarnation
from .threads import Thread
from .wake import derive_exact_reply_target
from .wire_watch import open_wire_watcher

AGENT_PREFIX = "!agent "
LIVE_DRAIN_INTERVAL = 0.05
WATCH_POLL_INTERVAL = 1.0
GOAL_WAIT_RECHECK_INTERVAL = 60.0


@dataclass(frozen=True, slots=True)
class QueuedInput:
    text: str
    echo: bool
    owner_created_at: float
    admission: int
    receipt: InputAttempt | None = None
    turn_id: str | None = None
    goal: Goal | None = None
    wait: GoalWait | None = None
    images: tuple[ImageInput, ...] = ()
    controller: Any = None

    def current(self, owner: Thread, admission: int, wait: GoalWait | None) -> bool:
        """A fresh input retains the exact owner/goal/wait seen at acceptance."""
        return (
            self.owner_created_at == owner.created_at
            and self.admission == admission
            and self.goal == owner.goal
            and self.wait == wait
        )


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
        self.forwarded_inputs: dict[str, set[str]] = {}
        self.steering_input_keys: dict[str, dict[str, str]] = {}
        self.steering_goal_ids: dict[str, dict[str, str | None]] = {}
        self.turn_input_keys: dict[str, set[str]] = {}
        self.turn_original_input_keys: dict[str, tuple[str, ...]] = {}
        self.selected_summary_admissions: dict[str, SelectedSummaryAdmission] = {}
        self.turn_input_text: dict[str, str] = {}
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
            self.queued_inputs.pop(session_id, None)
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
            OwnerIdentity(ThreadIncarnation(owner.name, owner.created_at), admission),
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
                if item.echo
                and item.owner_created_at == scope.owner_created_at
                and item.admission == scope.admission_generation
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
        queued_item: QueuedInput | None = None,
        *,
        client: Any = None,
    ) -> None:
        scope = self.queue_binding(session_id)
        if (
            queued_item is None
            or scope is None
            or (scope.owner_created_at, scope.admission_generation)
            != (queued_item.owner_created_at, queued_item.admission)
        ):
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
                field_meta=encode_updates(InputStartedUpdate(input_id, text, scope, revision)),
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

        async def loop() -> None:
            watcher = open_wire_watcher(self.comms.root)
            next_goal_wait_check = 0.0
            try:
                while True:
                    if watcher is None:
                        await asyncio.sleep(LIVE_DRAIN_INTERVAL)
                    else:
                        watcher.changed.clear()
                    try:
                        await self.drain_inbox(session_id)
                        await self.sessions.config.sync_thread(session_id)
                        if time.monotonic() >= next_goal_wait_check:
                            self.comms.goals.recover_closed_goal_wait(session_id)
                            next_goal_wait_check = time.monotonic() + GOAL_WAIT_RECHECK_INTERVAL
                        self.effects.turns.schedule_goal(session_id)
                        await self.sessions.config.refresh_auth_models()
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        # Never let the drain task die silently: a dead drain
                        # means replies stop reaching the client.
                        self.effects._debug_log(f"live-drain error: {error!r}")
                    if watcher is not None:
                        if watcher.invalid:
                            watcher.close()
                            watcher = None
                        else:
                            with suppress(TimeoutError):
                                async with asyncio.timeout(WATCH_POLL_INTERVAL):
                                    await watcher.changed.wait()
            finally:
                if watcher is not None:
                    watcher.close()

        context = self.runtime.controller.set(UNBOUND_CONTROLLER)
        try:
            task = asyncio.create_task(loop())
        finally:
            self.runtime.controller.reset(context)
        self.drain_tasks[session_id] = task

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
            session_id in self.effects.turns.active_turns,
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
        self.schedule_wake(session_id)
        return pushed

    def schedule_wake(self, session_id: str) -> None:
        if (
            self.closing
            or not self.auto_wake
            or not self.sessions.runtime_enabled
            or not self.pending_turns.get(session_id)
        ):
            return
        if session_id in self.wake_tasks and not self.wake_tasks[session_id].done():
            return

        async def wake() -> None:
            while self.pending_turns.get(session_id) and not self.closing:
                async with self.effects.turns.turn_locks.setdefault(session_id, asyncio.Lock()):
                    pending = self.pending_turns.pop(session_id, [])
                    owner = self.comms.registry.require(self.sessions.require(session_id))
                    if not self.comms.registry.status(owner.name).running:
                        continue
                    goal = owner.goal
                    if goal is not None and goal.state.active:
                        pending = [turn for turn in pending if turn.goal_id == goal.id]
                    if not pending:
                        continue
                    pending, remaining = ScheduledTurn.take_batch(pending)
                    if remaining:
                        self.pending_turns[session_id] = remaining
                    self.effects.turns.turn_tasks[session_id] = asyncio.current_task()  # type: ignore[assignment]
                    try:
                        await self.effects.turns.run_agent_turn(
                            session_id,
                            self.sessions.require(session_id),
                            "\n\n".join(turn.prompt for turn in pending),
                            reply_targets=tuple(
                                dict.fromkeys(
                                    target
                                    for turn in pending
                                    if (target := derive_exact_reply_target(turn.origin))
                                )
                            ),
                            origins=tuple(
                                turn.origin for turn in pending if turn.origin is not None
                            ),
                            autonomous_goal=bool(
                                len(pending) == 1
                                and pending[0].goal_id is not None
                                and pending[0].origin is None
                            ),
                            dependency_wait_id=pending[0].goal_wait_id,
                        )
                    except RequestError:
                        if pending[0].goal_wait_id is None or goal is None:
                            raise
                        self.comms.goals.block_goal_after_failed_turn(
                            owner.name,
                            started_goal=goal,
                            expected_worktree=owner.worktree,
                            diagnostic=(
                                "Standby wake launch authority unavailable; inspect the UNKNOWN "
                                "input before explicit Retry. No input was replayed."
                            ),
                        )
                        await self.effects.turns.sync_goal_execution(session_id, owner.name)
                    finally:
                        self.effects.turns.turn_tasks.pop(session_id, None)

        # Background work must not inherit a human controller from the task
        # that happened to schedule it. Only its own ACP prompt may bind one.
        context = self.runtime.controller.set(UNBOUND_CONTROLLER)
        try:
            self.wake_tasks[session_id] = asyncio.create_task(wake())
        finally:
            self.runtime.controller.reset(context)

    async def drain_count(self, session_id: str) -> int:
        return await self.drain_inbox(session_id)

    async def accept_followup(
        self,
        session_id: str,
        *,
        text: str,
        display_text: str,
        delivery: str,
        defer_display: bool,
        images: tuple[Any, ...],
    ) -> PromptResponse:
        inbox = self.backend_inboxes[session_id]
        pending_ids = self.forwarded_inputs.setdefault(session_id, set())
        if len(pending_ids) >= 32:
            raise RequestError.invalid_params(
                {"reason": "Too many follow-up inputs awaiting their own user start."}
            )
        input_id = uuid4().hex
        key = f"acp:{input_id}"
        with _store_lock(self.comms._wire_lock_path):
            snapshot = self.comms.registry.snapshot()
            owner = snapshot.aliases.get(
                self.sessions.require(session_id), self.sessions.require(session_id)
            )
            admission = snapshot.admission_generations[owner]
            admitted_goal = snapshot.threads[owner].goal
            self.steering_goal_ids.setdefault(session_id, {})[input_id] = (
                admitted_goal.id
                if admitted_goal is not None and admitted_goal.state.active
                else None
            )
            self.dispositions.record(
                key,
                seq=None,
                owner=owner,
                admission=admission,
                target=owner,
                text=display_text or text or "[image prompt]",
            )
            pending_ids.add(input_id)
            self.steering_input_keys.setdefault(session_id, {})[input_id] = key
            self.turn_input_keys.setdefault(session_id, set()).add(key)
            owner_row = snapshot.threads[owner]
            controller = self.runtime.controller.get()
            if controller is UNBOUND_CONTROLLER:
                controller = self.sessions.client
            self.queued_inputs.setdefault(session_id, {})[input_id] = QueuedInput(
                display_text,
                defer_display,
                owner_row.created_at,
                admission,
                self.dispositions.read().rows.get(key) if delivery == "queue" else None,
                owner_row.active_turn.id if owner_row.active_turn else None,
                owner_row.goal,
                self.comms.goals.goal_wait(owner),
                images,
                controller,
            )
            inbox.put_nowait(
                {
                    "type": "prompt",
                    "message": "User follow-up:\n" + text.removeprefix(AGENT_PREFIX),
                    "streamingBehavior": "steer",
                    "_input_id": input_id,
                    **({"images": [image.to_rpc() for image in images]} if images else {}),
                }
            )
            if delivery == "steer":
                inbox.put_nowait({"type": "interrupt_steering", "_input_ids": [input_id]})
        if delivery == "queue":
            await self.emit_queue_state(session_id)
        # ACP receipt is only local acceptance. The matching inputStarted
        # update, not this end_turn, is the model-read boundary.
        return PromptResponse(
            stop_reason="end_turn", field_meta=encode_updates(InputDeliveryChangedUpdate(input_id))
        )

    def bind_native_turn(
        self, session_id: str, owner: Thread, admission: int, turn_id: str
    ) -> asyncio.Queue[str | dict[str, Any]]:
        """Transfer existing live inputs to the new lease, never read them from disk."""
        inbox = self.backend_inboxes.setdefault(session_id, asyncio.Queue())
        wait = self.comms.goals.goal_wait(owner.name)
        for input_id, item in self.queued_inputs.get(session_id, {}).items():
            if item.current(owner, admission, wait):
                self.queued_inputs[session_id][input_id] = replace(item, turn_id=turn_id)
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
        for session_id, keys in self.turn_original_input_keys.items():
            if keys != (pending_input_key,) or self.sessions.bindings.get(session_id) != owner.name:
                continue
            for input_id, item in self.queued_inputs.get(session_id, {}).items():
                key = self.steering_input_keys.get(session_id, {}).get(input_id)
                if (
                    key is not None
                    and item.receipt is not None
                    and item.owner_created_at == owner.created_at
                    and item.admission == owner.active_turn.admission_generation
                    and item.turn_id == owner.active_turn.id
                ):
                    result[key] = item.receipt
        return result

    def send_now(self, session_id: str) -> None:
        with _store_lock(self.comms._wire_lock_path):
            inbox = self.backend_inboxes.get(session_id)
            queued = self.queued_inputs.get(session_id, {})
            if inbox is not None and queued:
                for key, item in tuple(queued.items()):
                    queued[key] = replace(item, receipt=None)
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
        original_keys: tuple[str, ...],
        initial_display_text: str | None,
    ) -> None:
        started_keys = (
            original_keys
            if input_id is None
            else (
                (steering_key,)
                if input_id is not None
                and (steering_key := self.steering_input_keys.get(session_id, {}).get(input_id))
                else ()
            )
        )
        for key in started_keys:
            row = self.dispositions.read().rows.get(key)
            if row is not None and not row.unresolved:
                await self.emit_input_disposition(session_id, row)
        if input_id is not None:
            self.forwarded_inputs.get(session_id, set()).discard(input_id)
        item = self.queued_inputs.get(session_id, {}).pop(input_id or "", None)
        await self.emit_input_started(
            session_id,
            (
                item.text
                if item and item.echo
                else initial_display_text
                if input_id is None
                else None
            ),
            input_id,
            queued_item=item,
        )
        await self.emit_queue_state(session_id)

    async def input_refused(self, session_id: str, input_id: str | None) -> None:
        if input_id is not None:
            self.forwarded_inputs.get(session_id, set()).discard(input_id)
            refused_key = self.steering_input_keys.get(session_id, {}).get(input_id)
            if refused_key is not None:
                # This input was denied before stdin.write. Keep
                # its persisted UNKNOWN row visible, but do not
                # count it as an unstarted sent follow-up.
                self.turn_input_keys.get(session_id, set()).discard(refused_key)
                await self.emit_input_delivery_changed(session_id)
            if self.queued_inputs.get(session_id, {}).pop(input_id, None):
                await self.emit_queue_state(session_id)

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
        self.forwarded_inputs.pop(session_id, None)
        self.turn_original_input_keys.pop(session_id, None)
        self.turn_input_text.pop(session_id, None)
        self.steering_input_keys.pop(session_id, None)
        self.steering_goal_ids.pop(session_id, None)
        self.turn_input_keys.pop(session_id, None)
        admission = self.selected_summary_admissions.pop(session_id, None)
        if admission is not None:
            admission.invalidate()
        await self.emit_input_delivery_changed(session_id)
        remaining = self.queued_inputs.pop(session_id, {})
        if remaining:
            self.restored_inputs.setdefault(session_id, {}).update(
                {key: replace(item, receipt=None) for key, item in remaining.items() if item.echo}
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
    ) -> None:
        key = f"acp:{uuid4().hex}"
        with _store_lock(self.comms._wire_lock_path):
            snapshot = self.comms.registry.snapshot()
            canonical = snapshot.aliases.get(thread_name, thread_name)
            admitted_goal = snapshot.threads[canonical].goal
            original_goal_id = (
                admitted_goal.id
                if admitted_goal is not None and admitted_goal.state.active
                else None
            )
            self.dispositions.record(
                key,
                seq=None,
                owner=canonical,
                admission=snapshot.admission_generations[canonical],
                target=canonical,
                text=display_text or task or "[image prompt]",
            )
        row = self.dispositions.read().rows.get(key)
        assert row is not None
        await self.emit_input_disposition(session_id, row)
        await self.effects.turns.run_agent_turn(
            session_id,
            thread_name,
            task,
            images=images,
            original_keys=(key,),
            initial_display_text=display_text,
            original_owner_input=True,
            original_goal_id=original_goal_id,
        )
