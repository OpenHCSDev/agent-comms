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

from . import backend
from .comms import Comms
from .coordination_store import (
    PublicationActivationBlocked,
)
from .declarations import (
    Message,
    ScheduledTurn,
    Thread,
    _store_lock,
)
from .input_disposition import AcpDeliveryCursors, FutureInputQueue, InputDispositions
from .input_effects import InputEffects
from .owner_lifecycle import OBSERVATION_INTERVAL
from .passive_channel_awareness import PassiveChannelAwareness
from .runtime import (
    UNBOUND_CONTROLLER,
    RuntimeServer,
)
from .selected_summary_admission import SelectedSummaryAdmission
from .session_lifecycle import SessionLifecycle
from .wire_watch import open_wire_watcher

AGENT_PREFIX = "!agent "
LIVE_DRAIN_INTERVAL = OBSERVATION_INTERVAL
WATCH_FALLBACK_INTERVAL = 1.0
GOAL_WAIT_RECHECK_INTERVAL = 60.0


@dataclass(frozen=True, slots=True)
class QueuedInput:
    text: str
    echo: bool
    owner_created_at: float
    admission: int
    receipt: dict[str, Any] | None = None
    turn_id: str | None = None


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
        self.steering_origins: dict[str, dict[str, Message]] = {}
        self.steering_goal_ids: dict[str, dict[str, str | None]] = {}
        self.turn_input_keys: dict[str, set[str]] = {}
        self.turn_original_input_keys: dict[str, tuple[str, ...]] = {}
        self.selected_summary_admissions: dict[str, SelectedSummaryAdmission] = {}
        self.turn_input_text: dict[str, str] = {}
        self.dispositions = InputDispositions(comms.root)
        self.delivery_cursors = AcpDeliveryCursors(comms.root)
        self.passive_awareness = PassiveChannelAwareness(comms.root)
        self.legacy_through: dict[str, int] = {}
        self.auto_wake = auto_wake
        self.pending_turns: dict[str, list[ScheduledTurn]] = {}
        self.direct_interrupt_tickets: dict[str, dict[str, str]] = {}
        self.drain_locks: dict[str, asyncio.Lock] = {}
        self.closing = False
        self.inbox_cursors: dict[str, int] = {}
        self.wake_tasks: dict[str, asyncio.Task[None]] = {}

    def initialize_session_delivery(
        self, session_id: str, thread: Thread, *, fresh: bool, private: bool
    ) -> None:
        if private:
            if not fresh:
                self.inbox_cursors.pop(session_id, None)
                self.legacy_through.pop(session_id, None)
            return
        cursor, legacy = self.delivery_cursors.initialize(
            self.comms.registry.aliases_for(thread.name),
            thread.name,
            high_water=self.comms.bus.latest_sequence(),
            fresh=fresh,
        )
        with _store_lock(self.comms._wire_lock_path):
            owner = self.comms.registry.require(thread.name)
            admission = self.comms.registry.snapshot().admission_generations[owner.name]
            # Advisory awareness must not fail a committed session attach.
            with suppress(OSError, TypeError, ValueError):
                self.passive_awareness.initialize(
                    owner,
                    admission=admission,
                    high_water=self.comms.bus.latest_sequence(),
                    channels=self.comms.channels.catalog.targets_for(owner.tags),
                    fresh=fresh,
                )
        self.inbox_cursors[session_id] = cursor
        self.legacy_through[session_id] = legacy

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

    def queue_binding(self, session_id: str) -> dict[str, Any] | None:
        try:
            owner, admission = self.comms.registry.live_owner_with_admission(
                self.sessions.bindings.get(session_id, session_id)
            )
        except (OSError, ValueError):
            return None
        return {
            "version": 1,
            "sessionId": session_id,
            "ownerThread": owner.name,
            "ownerCreatedAt": owner.created_at,
            "ownerEpoch": admission,
            "admissionGeneration": admission,
        }

    def queue_state(self, session_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Bounded exact-ID presentation of this ACP owner's in-memory queue.

        A changed owner admission never inherits old in-memory queue entries.
        They and their durable input dispositions remain untouched/UNKNOWN.
        An oversized projection is unavailable, not an empty queue or retry.
        """
        revision = self.queue_revisions.get(session_id, 0) + 1
        self.queue_revisions[session_id] = revision
        binding = self.queue_binding(session_id)
        if binding is None:
            return None, None
        scope = {key: value for key, value in binding.items() if key != "version"}

        def current(values: dict[str, QueuedInput]) -> list[dict[str, str]]:
            return [
                {"inputId": input_id, "text": item.text}
                for input_id, item in values.items()
                if item.echo
                and item.owner_created_at == binding["ownerCreatedAt"]
                and item.admission == binding["admissionGeneration"]
            ]

        items = current(self.queued_inputs.get(session_id, {}))
        restored = current(self.restored_inputs.get(session_id, {}))
        if len(items) + len(restored) > 32:
            return binding, None
        if any(type(row["text"]) is not str for row in items + restored):
            # Historical malformed entries retain their exact IDs and UNKNOWN
            # dispositions, but cannot be projected as a valid queue state.
            return binding, None
        try:
            sizes = [len(row["text"].encode("utf-8")) for row in items + restored]
        except UnicodeError:
            # JSON permits lone surrogates. They remain exact queued/UNKNOWN
            # inputs, but cannot be advertised as a valid UTF-8 queue row.
            return binding, None
        if any(size > 4096 for size in sizes) or sum(sizes) > 65536:
            return binding, None
        return binding, {
            "version": 1,
            "scope": scope,
            "revision": revision,
            "items": items,
            "restored": restored,
        }

    async def emit_queue_state(
        self, session_id: str, *, restored: list[str] | None = None, client: Any = None
    ) -> None:
        binding, state = self.queue_state(session_id)
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={
                    "agentComms": {
                        "queueBinding": binding,
                        "queueState": state,
                        # Legacy text-only projection is informational, never
                        # authoritative for exact-ID queue matching.
                        "queue": [row["text"] for row in state["items"]] if state else [],
                        "restored": (
                            [row["text"] for row in state["restored"]]
                            if state is not None and restored is not None
                            else []
                        ),
                    }
                },
            ),
        )

    async def emit_input_started(
        self,
        session_id: str,
        text: str | None,
        input_id: str | None = None,
        queued_item: QueuedInput | None = None,
    ) -> None:
        proof: dict[str, Any] = {"text": text}
        if input_id is not None:
            proof["inputId"] = input_id
        if input_id is not None and queued_item is not None:
            binding = self.queue_binding(session_id)
            if binding is not None and (
                binding["ownerCreatedAt"],
                binding["admissionGeneration"],
            ) == (queued_item.owner_created_at, queued_item.admission):
                revision = self.queue_revisions.get(session_id, 0) + 1
                self.queue_revisions[session_id] = revision
                proof.update(
                    version=1,
                    scope={key: value for key, value in binding.items() if key != "version"},
                    revision=revision,
                )
        await self.runtime.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={"agentComms": {"inputStarted": proof}},
            ),
        )

    async def emit_input_disposition(
        self, session_id: str, row: dict[str, Any], client: Any = None
    ) -> None:
        await self.emit_public_input_disposition(session_id, InputDispositions.public(row), client)

    async def emit_public_input_disposition(
        self, session_id: str, disposition: dict[str, Any], client: Any = None
    ) -> None:
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={"agentComms": {"inputDisposition": disposition}},
            ),
        )

    async def emit_input_delivery_changed(self, session_id: str) -> None:
        """Invalidate attached views after a notice-only owner action."""
        await self.runtime.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={"agentComms": {"inputDeliveryChanged": True}},
            ),
        )

    async def replay_unknown_inputs(self, session_id: str, client: Any = None) -> None:
        owner = self.sessions.require(session_id)
        overview = self.comms.goals.input_delivery(
            owner, awaiting_keys=self.awaiting_input_keys(session_id)
        )
        for disposition in overview["inputs"]:
            await self.emit_public_input_disposition(session_id, disposition, client=client)

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
                                async with asyncio.timeout(WATCH_FALLBACK_INTERVAL):
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

    async def drain_owned_inbox(self, session_id: str) -> int:
        # The legacy ACP display cursor/ACK/steer path is not a native input
        # receipt. Never let it consume an explicitly cut-over private bus.
        if private_root := self.effects._private_nk_marker():
            return await self.effects._drain_private_nk(session_id, private_root)
        if self.effects._private_nk_native_package is not None:
            raise PublicationActivationBlocked("configured private N/K ACP has no durable marker")
        thread_name = await self.sessions.sync_identity(session_id)
        if self.comms.registry.status(thread_name).stopped:
            return 0
        backend_inbox = self.backend_inboxes.get(session_id)
        if (
            self.sessions.client is None
            and backend_inbox is None
            and not self.sessions.runtime_enabled
        ):
            return 0
        pushed = 0
        after = self.inbox_cursors.get(session_id, 0)
        high_water = self.comms.bus.latest_sequence()
        page = self.comms.bus.incoming_page(thread_name, after=after) if after < high_water else None
        # A private cutover on a previously empty bus may have occurred after
        # the first classification but before this page was read. Reclassify
        # before touching delivery cursors, input dispositions or legacy ACK.
        if private_root := self.effects._private_nk_marker():
            return await self.effects._drain_private_nk(session_id, private_root)
        incoming_messages = page.messages if page else ()
        for message in incoming_messages:
            admitted = True
            dependency_wait = None
            row: dict[str, Any] | None = None
            with _store_lock(self.comms._wire_lock_path):
                snapshot = self.comms.registry.snapshot()
                current_name = snapshot.aliases.get(thread_name, thread_name)
                current = snapshot.threads[current_name]
                status = snapshot.statuses[current_name]
                aliases = frozenset(
                    {
                        current_name,
                        *(
                            alias
                            for alias, target in snapshot.aliases.items()
                            if target == current_name
                        ),
                    }
                )
                incoming = ScheduledTurn.incoming(message, aliases=snapshot.aliases)
                starts_turn = message.starts_turn_for(current_name, aliases=snapshot.aliases)
                direct = starts_turn and message.target in aliases
                if starts_turn:
                    wait = self.comms.goals.goal_wait(current_name) if direct else None
                    if wait is not None and wait.matches(message, snapshot):
                        dependency_wait = wait
                        incoming = replace(
                            incoming, goal_id=wait.goal_id, goal_wait_id=wait.wait_id
                        )
                    elif direct and current.goal is not None and current.goal.state.active:
                        # A NEW direct DM may interrupt the goal without being
                        # a declared dependency reply or a goal continuation.
                        incoming = replace(
                            incoming,
                            direct_interrupt_goal_id=current.goal.id,
                            direct_interrupt_goal_revision=current.goal.revision,
                            direct_interrupt_wait_id=wait.wait_id if wait else None,
                        )
                    key = self.dispositions.bus_key(message, current)
                    admitted = self.dispositions.record(
                        key,
                        seq=message.seq,
                        owner=current_name,
                        admission=snapshot.admission_generations[current_name],
                        target=message.target,
                        text=incoming.prompt,
                    )
                    row = self.dispositions.get(key)
                    admitted = (
                        admitted
                        and message.seq > self.legacy_through.get(session_id, 0)
                        and status.running
                        and (
                            current.goal is None
                            or not current.goal.state.active
                            or dependency_wait is not None
                            or incoming.direct_interrupt_goal_id is not None
                        )
                        and backend.rpc_args_for(
                            self.effects.turns.agent_bin, self.effects.turns.agent_args
                        )
                        is not None
                    )
                    if (
                        admitted
                        and incoming.direct_interrupt_goal_id is not None
                        and self.auto_wake
                        and self.sessions.runtime_enabled
                    ):
                        ticket = uuid4().hex
                        self.direct_interrupt_tickets.setdefault(session_id, {})[key] = ticket
                        incoming = replace(
                            incoming,
                            direct_interrupt_input_key=key,
                            direct_interrupt_ticket=ticket,
                        )
                self.delivery_cursors.advance(aliases, message.seq)
                self.inbox_cursors[session_id] = message.seq
            if row is not None and row["status"] == "unknown":
                await self.emit_input_disposition(session_id, row)
            if (
                admitted
                and dependency_wait is None
                and incoming.direct_interrupt_goal_id is None
                and backend_inbox is not None
                and starts_turn
                and incoming.reply_target is None
            ):
                input_id = f"bus-{message.seq}"
                self.steering_origins.setdefault(session_id, {})[input_id] = message
                self.steering_input_keys.setdefault(session_id, {})[input_id] = key
                self.turn_input_keys.setdefault(session_id, set()).add(key)
                self.forwarded_inputs.setdefault(session_id, set()).add(input_id)
                backend_inbox.put_nowait(
                    {
                        "type": "prompt",
                        "message": incoming.prompt,
                        "streamingBehavior": "steer",
                        "_input_id": input_id,
                    }
                )
            elif admitted and self.auto_wake and self.sessions.runtime_enabled and starts_turn:
                self.pending_turns.setdefault(session_id, []).append(incoming)
            await self.runtime.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(
                        type="text", text=f"Incoming from {message.sender}:\n{message.body}\n"
                    ),
                    field_meta={
                        "agentComms": {
                            "incoming": {
                                "sender": message.sender,
                                "target": message.target,
                                "body": message.body,
                                "sequence": message.seq,
                            }
                        }
                    },
                ),
            )
            pushed += 1
        if page is not None and not page.has_newer:
            aliases = self.comms.registry.aliases_for(thread_name)
            self.delivery_cursors.advance(aliases, high_water)
            self.inbox_cursors[session_id] = max(self.inbox_cursors.get(session_id, 0), high_water)
        if pushed:
            self.comms.messaging.acknowledge_through(thread_name, self.inbox_cursors[session_id])
            await self.emit_input_delivery_changed(session_id)
        self.schedule_wake(session_id)
        return pushed

    def forget_direct_interrupt(self, session_id: str, turn: ScheduledTurn) -> None:
        tickets = self.direct_interrupt_tickets.get(session_id, {})
        if turn.direct_interrupt_input_key and tickets.get(turn.direct_interrupt_input_key) == (
            turn.direct_interrupt_ticket
        ):
            tickets.pop(turn.direct_interrupt_input_key, None)

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
                        # The durable UNKNOWN rows remain visible. A stopped
                        # owner cannot launch a turn from this old wake queue.
                        for turn in pending:
                            self.forget_direct_interrupt(session_id, turn)
                        continue
                    goal = owner.goal
                    old_pending = pending
                    if goal is not None and goal.state.active:
                        pending = [
                            turn
                            for turn in pending
                            if (turn.goal_id == goal.id or turn.direct_interrupt_goal_id == goal.id)
                            and turn.still_current_interrupt(goal)
                            # Fresh admission at dispatch: only a provably
                            # unattempted disposition may still launch. An
                            # attempted/historical input is dropped, never
                            # replayed, and its ticket is discarded.
                            and (
                                turn.direct_interrupt_goal_id is None
                                or (
                                    (
                                        row := self.dispositions.get(
                                            turn.direct_interrupt_input_key or ""
                                        )
                                    )
                                    is not None
                                    and row["status"] == "unknown"
                                    and row["native_id"] is None
                                )
                            )
                        ]
                    else:
                        # No active goal: ordinary interrupts are invalid now
                        # (their goal is gone/cleared); discard them and their
                        # tickets instead of dispatching parked-goal framing.
                        active_pending = [
                            turn for turn in pending if turn.direct_interrupt_goal_id is None
                        ]
                        for turn in pending:
                            if turn not in active_pending:
                                self.forget_direct_interrupt(session_id, turn)
                        pending = active_pending
                    for turn in old_pending:
                        if turn not in pending:
                            self.forget_direct_interrupt(session_id, turn)
                    if not pending:
                        continue
                    pending, remaining = ScheduledTurn.take_batch(pending)
                    if remaining:
                        self.pending_turns[session_id] = remaining
                    if pending[0].direct_interrupt_goal_id is not None:
                        # Rebind fresh expectations AT DISPATCH: the queue may
                        # have survived benign same-goal bumps, but the turn
                        # must then pin the CURRENT goal revision and wait ID
                        # and hold them exactly through the native send lock.
                        # Any change after dispatch denies without retry.
                        current_wait = self.comms.goals.goal_wait(owner.name)
                        assert goal is not None  # filtered above: goal active
                        pending[0] = replace(
                            pending[0],
                            direct_interrupt_goal_revision=goal.revision,
                            direct_interrupt_wait_id=(
                                current_wait.wait_id if current_wait else None
                            ),
                        )
                    self.effects.turns.turn_tasks[session_id] = asyncio.current_task()  # type: ignore[assignment]
                    try:
                        await self.effects.turns.run_agent_turn(
                            session_id,
                            self.sessions.require(session_id),
                            "\n\n".join(turn.prompt for turn in pending),
                            reply_targets=tuple(
                                dict.fromkeys(
                                    turn.reply_target for turn in pending if turn.reply_target
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
                            direct_interrupt_goal_id=pending[0].direct_interrupt_goal_id,
                            direct_interrupt_goal_revision=pending[
                                0
                            ].direct_interrupt_goal_revision,
                            direct_interrupt_wait_id=pending[0].direct_interrupt_wait_id,
                            direct_interrupt_input_key=pending[0].direct_interrupt_input_key,
                            direct_interrupt_ticket=pending[0].direct_interrupt_ticket,
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
            if delivery == "queue":
                self.queued_inputs.setdefault(session_id, {})[input_id] = QueuedInput(
                    display_text,
                    defer_display,
                    owner_row.created_at,
                    admission,
                    self.dispositions.get(key),
                    owner_row.active_turn.id if owner_row.active_turn else None,
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
            stop_reason="end_turn",
            field_meta={
                "agentComms": {
                    "inputDisposition": {
                        "inputId": input_id,
                        "status": "accepted_not_started",
                        "delivery": delivery,
                    }
                }
            },
        )

    def future_inputs(
        self, owner: Thread, pending_input_key: str | None
    ) -> dict[str, dict[str, Any]]:
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
        self.direct_interrupt_tickets.clear()
        await asyncio.gather(*self.wake_tasks.values(), return_exceptions=True)
        self.wake_tasks.clear()

    async def close(self) -> None:
        tasks = tuple(self.drain_tasks.values())
        self.drain_tasks.clear()
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
            row = self.dispositions.get(key)
            if row is not None and row["status"] == "started":
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
        self.steering_origins.pop(session_id, None)
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
            await self.emit_queue_state(
                session_id, restored=[item.text for item in remaining.values() if item.echo]
            )

    async def run_owned_input(
        self,
        session_id: str,
        thread_name: str,
        task: str,
        *,
        images: tuple[Any, ...] = (),
        display_text: str | None = None,
    ) -> None:
        if (
            backend.rpc_args_for(self.effects.turns.agent_bin, self.effects.turns.agent_args)
            is None
        ):
            # The plain text fallback has no Pi native input-ID protocol.
            # Preserve its existing local command behavior without attaching
            # a false Pi start claim to it.
            await self.effects.turns.run_agent_turn(session_id, thread_name, task, images=images)
            return
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
        row = self.dispositions.get(key)
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
