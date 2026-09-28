"""Single owner-turn admission, native stream and terminal settlement."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp import RequestError

from . import backend
from .declarations import (
    Message,
    MessageRoute,
    RelationViolationError,
    ScheduledTurn,
    TurnRouting,
    _store_lock,
    is_channel_target,
)
from .goal_attempts import (
    Generation,
    GoalAttemptError,
    LaunchPermit,
)
from .runtime import (
    UNBOUND_CONTROLLER,
)
from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission
from .turn_progress import TurnProgress
from .turn_runner import _goal_attempt_unavailable

if TYPE_CHECKING:
    from .turn_runner import TurnRunner


class OwnedTurn:
    """One admitted owner turn; stable source facts and native callbacks live here."""

    def __init__(
        self,
        runner: TurnRunner,
        session_id: str,
        thread_name: str,
        task: str,
        *,
        reply_targets: tuple[str, ...] = (),
        origins: tuple[Message, ...] = (),
        images: tuple[Any, ...] = (),
        original_keys: tuple[str, ...] = (),
        initial_display_text: str | None = None,
        autonomous_goal: bool = False,
        original_owner_input: bool = False,
        original_goal_id: str | None = None,
        dependency_wait_id: str | None = None,
        direct_interrupt_goal_id: str | None = None,
        direct_interrupt_goal_revision: int | None = None,
        direct_interrupt_wait_id: str | None = None,
        direct_interrupt_input_key: str | None = None,
        direct_interrupt_ticket: str | None = None,
    ) -> None:
        self.runner = runner
        self.session_id = session_id
        self.thread_name = thread_name
        self.task = task
        self.reply_targets = reply_targets
        self.origins = origins
        self.images = images
        self.original_keys = original_keys
        self.initial_display_text = initial_display_text
        self.autonomous_goal = autonomous_goal
        self.original_owner_input = original_owner_input
        self.original_goal_id = original_goal_id
        self.dependency_wait_id = dependency_wait_id
        self.direct_interrupt_goal_id = direct_interrupt_goal_id
        self.direct_interrupt_goal_revision = direct_interrupt_goal_revision
        self.direct_interrupt_wait_id = direct_interrupt_wait_id
        self.direct_interrupt_input_key = direct_interrupt_input_key
        self.direct_interrupt_ticket = direct_interrupt_ticket

    def admit(self):
        self.original_display = (
            None if self.autonomous_goal else (self.initial_display_text or self.task)
        )
        self.owner_task = asyncio.current_task()
        assert self.owner_task is not None
        self.thread = self.runner.comms.registry.require(self.thread_name)
        self.thread_name = self.thread.name
        if backend.rpc_args_for(self.runner.agent_bin, self.runner.agent_args) is None and any(
            origin.seq > 0 for origin in self.origins
        ):
            # A text backend has no native user-start receipt. Its process
            # must not run for a bus input whose UNKNOWN row needs that proof.
            return
        self.goal = self.thread.goal
        self.wait = self.runner.comms.goal_wait(self.thread_name)
        self.direct_interrupt = self.direct_interrupt_goal_id is not None
        if self.direct_interrupt:
            self.tickets = self.runner.inputs.direct_interrupt_tickets.get(self.session_id, {})
            if (
                self.direct_interrupt_input_key is None
                or self.direct_interrupt_ticket is None
                or self.tickets.get(self.direct_interrupt_input_key) != self.direct_interrupt_ticket
            ):
                return
            self.tickets.pop(
                self.direct_interrupt_input_key, None
            )  # one admission, one turn at most
            # A typed interruption must be one NEW exact direct input whose
            # goal revision and wait ID were captured AT DISPATCH; any change
            # after dispatch denies without retry. Benign bumps before
            # dispatch are handled by the dispatcher's fresh rebind.
            self.aliases = self.runner.comms.registry.aliases_for(self.thread_name)
            if (
                self.autonomous_goal
                or self.original_owner_input
                or self.dependency_wait_id is not None
                or self.goal is None
                or not self.goal.active
                or self.goal.id != self.direct_interrupt_goal_id
                or self.goal.revision != self.direct_interrupt_goal_revision
                or (self.wait.wait_id if self.wait else None) != self.direct_interrupt_wait_id
                or len(self.origins) != 1
                or self.origins[0].seq <= 0
                or self.origins[0].target not in self.aliases
                or self.direct_interrupt_input_key is None
                or self.original_keys
                or self.direct_interrupt_input_key
                != self.runner.inputs.dispositions.bus_key(self.origins[0], self.thread)
            ):
                return
            self.row = self.runner.inputs.dispositions.get(self.direct_interrupt_input_key)
            if (
                self.row is None
                or self.row["status"] != "unknown"
                or self.row["native_id"] is not None
                or self.row["sequence"] != self.origins[0].seq
            ):
                return
        if (
            self.wait is not None
            and not self.original_owner_input
            and not self.direct_interrupt
            and self.wait.wait_id != self.dependency_wait_id
        ):
            return
        if self.dependency_wait_id is not None and (
            self.wait is None or self.wait.wait_id != self.dependency_wait_id
        ):
            return
        if self.original_owner_input and self.original_goal_id != (
            self.goal.id if self.goal is not None and self.goal.active else None
        ):
            raise RequestError.invalid_params({"reason": "input_authority_changed"})
        self.goal_permit: LaunchPermit | None = None
        if self.autonomous_goal and (self.goal is None or not self.goal.active):
            return
        if self.goal is not None and self.goal.active and not self.direct_interrupt:
            if backend.rpc_args_for(self.runner.agent_bin, self.runner.agent_args) is None:
                if self.autonomous_goal:
                    return
                raise RequestError.invalid_params({"reason": "goal_requires_native_pi"})
            self.store = self.runner.goal_store
            if (
                self.store is None
                and (self.runner.comms.root / "goal-private" / "goal_attempts.sqlite3").exists()
            ):
                self.store = self.runner.open_goal_store()
            if self.store is None:
                if self.autonomous_goal:
                    return
                raise _goal_attempt_unavailable()
            self.admission = self.runner.comms.registry.snapshot().admission_generations[
                self.thread_name
            ]
            with _store_lock(self.runner.comms._wire_lock_path):
                self.generation = self.store.snapshot(self.goal.id)
                if self.generation is None or not self.generation.lifecycle.ready:
                    if self.autonomous_goal:
                        return
                    raise _goal_attempt_unavailable()
                try:
                    self.grant = self.runner.ready_goal_grant_locked(
                        self.thread, self.admission, self.store, self.generation
                    )
                    self.reservation = self.store.reserve(
                        self.goal.id, self.generation.number, ready_grant=self.grant
                    )
                    self.goal_permit = self.store.claim_launch(self.reservation)
                except GoalAttemptError as error:
                    if self.autonomous_goal:
                        return
                    raise _goal_attempt_unavailable() from error
        self.runner.sessions.bindings[self.session_id] = self.thread_name
        return True

    def begin(self):
        self.runner.emitted_errors.pop(self.session_id, None)
        self.turn_id = uuid4().hex
        self.routing = TurnRouting(
            self.origins,
            MessageRoute(self.thread_name, self.reply_targets) if self.reply_targets else None,
        )
        self.checkpoint = self.runner.comms.transcript_checkpoint(self.thread_name)
        self.turn_claim = self.runner.comms.begin_turn(
            self.thread_name, self.turn_id, self.task[:80], self.routing
        )
        self.turn_admission = self.runner.comms.registry.snapshot().admission_generations[
            self.thread_name
        ]
        self.direct_origins = tuple(
            origin
            for origin in self.origins
            if origin.seq > 0
            and origin.target in self.runner.comms.registry.aliases_for(self.thread_name)
        )
        self.bus_origins = tuple(origin for origin in self.origins if origin.seq > 0)
        if self.bus_origins:
            with _store_lock(self.runner.comms._wire_lock_path):
                self.snapshot = self.runner.comms.registry.snapshot()
                for origin in self.bus_origins:
                    self.key = self.runner.inputs.dispositions.bus_key(origin, self.thread)
                    if self.runner.inputs.dispositions.get(self.key) is None:
                        self.runner.inputs.dispositions.record(
                            self.key,
                            seq=origin.seq,
                            owner=self.thread_name,
                            admission=self.snapshot.admission_generations[self.thread_name],
                            target=origin.target,
                            text=ScheduledTurn.incoming(
                                origin, aliases=self.snapshot.aliases
                            ).prompt,
                        )
                    self.original_keys = (*self.original_keys, self.key)
        self.runner.inputs.turn_input_keys[self.session_id] = set(self.original_keys)
        self.runner.inputs.steering_input_keys[self.session_id] = {}
        self.runner.inputs.steering_origins[self.session_id] = {}
        self.runner.inputs.steering_goal_ids[self.session_id] = {}

        self.passive_frame = ""
        self.passive_sources: tuple[tuple[int, str, str], ...] = ()
        self.channel_batch = (
            len(self.origins) > 1
            and len({origin.seq for origin in self.origins}) == len(self.origins)
            and all(origin.seq > 0 and is_channel_target(origin.target) for origin in self.origins)
            and self.original_keys
            == tuple(
                self.runner.inputs.dispositions.bus_key(origin, self.thread)
                for origin in self.origins
            )
            # The durable admission owns the exact prompt, including the
            # names resolved at admission. Re-deriving it here can drift if
            # a recipient was renamed before or after inbox draining.
            and (admitted_texts := self.runner.inputs.dispositions.source_texts(self.original_keys))
            is not None
            and self.task == "\n\n".join(admitted_texts)
        )

    def input_keys_valid(self, public_id: str | None, keys: tuple[str, ...], text: str) -> bool:
        # One authoritative user start proves every sequence in an exact
        # channel batch. Never credit an omitted/reordered original input,
        # a mixed direct batch, or a differently transformed native prompt.
        return len(keys) <= 1 or (
            public_id is None
            and self.channel_batch
            and keys == self.original_keys
            and text == self.task
        )

    @contextmanager
    def send_boundary(
        self, public_id: str | None, native_id: str, sent_text: str, *, already_bound: bool = False
    ) -> Iterator[bool | None]:
        # This lock spans the final authority read and stdin.write only.
        # Pi's turn, ACK, and provider response happen after it is released.
        with _store_lock(self.runner.comms._wire_lock_path):
            self.runner.comms.maintenance.assert_open_unlocked()
            snapshot = self.runner.comms.registry.snapshot()
            canonical = snapshot.aliases.get(self.thread_name, self.thread_name)
            current = snapshot.threads.get(canonical)
            current_goal = current.goal if current is not None else None
            current_wait = self.runner.comms.goal_wait(canonical) if current is not None else None
            if self.goal is not None and self.goal.active:
                goal_ok = (
                    current_goal is not None
                    and current_goal.id == self.goal.id
                    and current_goal.active
                )
            else:
                goal_ok = current_goal is None or not current_goal.active
            # A parked-goal DM owns no goal attempt. A fresh owner input
            # may join that SAME interruption, not borrow or retry the goal.
            interrupt_scope_current = (
                self.direct_interrupt
                and current_goal is not None
                and current_goal.active
                and current_goal.id == self.direct_interrupt_goal_id
                and current_goal.revision == self.direct_interrupt_goal_revision
                and (current_wait.wait_id if current_wait else None)
                == self.direct_interrupt_wait_id
            )
            owner_interrupt_followup = False
            input_permit = self.goal_permit
            admitted_goals = self.runner.inputs.steering_goal_ids.get(self.session_id, {})
            owner_followup = public_id is not None and public_id in admitted_goals
            if owner_followup:
                assert public_id is not None
                admitted_goal_id = admitted_goals[public_id]
                current_goal_id = (
                    current_goal.id if current_goal is not None and current_goal.active else None
                )
                goal_ok = admitted_goal_id == current_goal_id
                input_permit = (
                    self.goal_permit
                    if self.goal_permit is not None
                    and self.goal_permit.reservation.goal_id == admitted_goal_id
                    else self.progress.originated_attempts.get(admitted_goal_id or "")
                )
                if current_goal_id is not None and input_permit is None:
                    owner_interrupt_followup = interrupt_scope_current and goal_ok
                    goal_ok = owner_interrupt_followup
            keys = (
                self.original_keys
                if public_id is None
                else (
                    (key,)
                    if (
                        key := self.runner.inputs.steering_input_keys.get(self.session_id, {}).get(
                            public_id
                        )
                    )
                    else ()
                )
            )
            if owner_interrupt_followup:
                # The permitless exception requires this exact fresh ACP
                # admission; an absent/foreign mapping cannot skip binding.
                owner_interrupt_followup = keys == (f"acp:{public_id}",)
                goal_ok = owner_interrupt_followup
            interrupt_ok = (
                interrupt_scope_current
                and public_id is None
                and len(self.direct_origins) == 1
                and len(keys) == 1
                and current is not None
                and keys[0] == self.direct_interrupt_input_key
                and keys[0]
                == self.runner.inputs.dispositions.bus_key(self.direct_origins[0], current)
            )
            owner_ok = (
                current is not None
                and self.input_keys_valid(public_id, keys, sent_text)
                and snapshot.statuses[canonical].running
                and snapshot.admission_generations.get(canonical) == self.turn_admission
                and current.pid == self.thread.pid
                and current.created_at == self.thread.created_at
                and current.worktree == self.thread.worktree
                and current.active_turn is not None
                and current.active_turn.id == self.turn_id
            )
            if owner_ok and public_id is None and self.passive_frame:
                assert current is not None
                try:
                    owner_ok = self.runner.inputs.passive_awareness.still_current(
                        current,
                        snapshot,
                        self.runner.comms.channel_catalog.targets_for(current.tags),
                        self.passive_sources,
                    )
                except (OSError, TypeError, ValueError):
                    owner_ok = False  # No stale advisory frame crosses native start.
            # A newly activated goal may supersede a follow-up that has
            # not yet been sent. Owner revocation still ends the turn.
            defer_for_goal = (
                public_id is not None
                and owner_ok
                and (self.goal is None or not self.goal.active)
                and current_goal is not None
                and current_goal.active
            )
            allowed = (
                owner_ok
                and goal_ok
                and (
                    current_wait is None
                    or owner_followup
                    or (public_id is None and self.original_owner_input)
                    or interrupt_ok
                    or (
                        public_id is None
                        and current_wait.wait_id == self.dependency_wait_id
                        and any(
                            current_wait.matches(origin, snapshot) for origin in self.direct_origins
                        )
                    )
                )
                and not (
                    self.dependency_wait_id is not None
                    and public_id is None
                    and (current_wait is None or current_wait.wait_id != self.dependency_wait_id)
                )
                and not (
                    keys
                    and current_goal is not None
                    and current_goal.active
                    and not owner_followup
                    and not (
                        public_id is None
                        and (
                            self.original_owner_input
                            or self.dependency_wait_id is not None
                            or interrupt_ok
                        )
                    )
                )
            )
            selected_admission = (
                self.runner.inputs.selected_summary_admissions.get(self.session_id)
                if public_id is None
                else None
            )
            if allowed and selected_admission is None:
                from .compaction_send_admission import native_input_admitted

                # Under the same wire lock as the owner commit. A selected
                # row blocks this ordinary path regardless of its status.
                allowed = current is not None and native_input_admitted(
                    self.runner.comms.root, current.session_file
                )
            if allowed and input_permit is not None:
                attempt = input_permit.reservation
                assert self.runner.goal_store is not None
                allowed = self.runner.goal_store._is_attempt(
                    attempt,
                    "claimed",
                    Generation(
                        attempt.goal_id,
                        attempt.generation,
                        "reserved",
                        attempt.attempt_id,
                    ),
                )
            if not allowed and selected_admission is not None:
                selected_admission.invalidate()  # No later owner/turn ABA can revive it.
            if allowed and selected_admission is not None:
                # The only selected bypass is a post-fsync-ACK ephemeral
                # one-shot, consumed at this same durable native-ID bind
                # point under the wire lock, before any stdin.write. No
                # admission is reconstructed from SQLite.
                if (
                    current is None
                    or current.session_file is None
                    or len(keys) != 1
                    or already_bound
                ) or (revision := backend._session_revision(current.session_file)) is None:
                    selected_admission.invalidate()
                    allowed = False
                else:
                    original = self.runner.inputs.dispositions.get(keys[0])
                    if original is None or type(original["source_text"]) is not str:
                        selected_admission.invalidate()
                        allowed = False
                    else:
                        digest = hashlib.sha256(sent_text.encode()).hexdigest()
                        original_digest = hashlib.sha256(
                            original["source_text"].encode()
                        ).hexdigest()
                        identity = SelectedAdmissionIdentity(
                            owner_name=canonical,
                            owner_pid=current.pid,
                            owner_created_at=float(current.created_at).hex(),
                            turn_id=self.turn_id,
                            ingress_key=keys[0],
                            admission_generation=snapshot.admission_generations[canonical],
                            correction_witness=(
                                f"{snapshot.admission_generations[canonical]}:{digest}"
                            ),
                            input_sha256=digest,
                            original_sha256=original_digest,
                            reserved_revision=selected_admission._identity.reserved_revision,
                            session_revision=revision,
                        )
                        try:
                            self.runner.inputs.dispositions.compaction_rows(
                                current, keys[0], self.runner.inputs
                            )
                        except RelationViolationError:
                            selected_admission.invalidate()
                        allowed = selected_admission.consume_bound_original(
                            wire_root=self.runner.comms.root,
                            session_file=current.session_file,
                            identity=identity,
                            native_id=native_id,
                            sent_text=sent_text,
                            dispositions=self.runner.inputs.dispositions,
                        )
            elif allowed:
                for key in keys:
                    row = self.runner.inputs.dispositions.get(key)
                    if (
                        row is None
                        or row["status"] != "unknown"
                        or row["admission"] != snapshot.admission_generations[canonical]
                        or not (
                            row["native_id"] == native_id
                            and row["turn_id"] == self.turn_id
                            and row["sent_text"] == sent_text
                            if already_bound
                            else self.runner.inputs.dispositions.bind(
                                key,
                                admission=row["admission"],
                                turn_id=self.turn_id,
                                native_id=native_id,
                                text=sent_text,
                            )
                        )
                    ):
                        allowed = False
                        break
            if (
                allowed
                and current_wait is not None
                and not interrupt_ok
                and not owner_interrupt_followup
            ):
                allowed = self.runner.comms.consume_goal_wait(canonical, current_wait.wait_id)
            if allowed:
                if public_id is None:
                    display = self.original_display
                    input_origins = self.origins
                else:
                    row = self.runner.inputs.dispositions.get(keys[0]) if keys else None
                    display = row["source_text"] if row is not None else sent_text
                    origin = self.runner.inputs.steering_origins.get(self.session_id, {}).get(
                        public_id
                    )
                    input_origins = (origin,) if origin is not None else ()
                self.runner.comms.record_input_display(
                    native_id,
                    display,
                    sent_text=sent_text,
                    routing=TurnRouting(input_origins, None) if input_origins else None,
                )
            yield True if allowed else None if defer_for_goal else False

    def native_start(self, public_id: str | None, native_id: str, sent_text: str) -> bool:
        keys = (
            self.original_keys
            if public_id is None
            else (
                (key,)
                if (
                    key := self.runner.inputs.steering_input_keys.get(self.session_id, {}).get(
                        public_id
                    )
                )
                else ()
            )
        )
        return self.input_keys_valid(public_id, keys, sent_text) and all(
            self.runner.inputs.dispositions.started(
                key, turn_id=self.turn_id, native_id=native_id, text=sent_text
            )
            for key in keys
        )

    def prepare_prompt(self):
        self.worktree = (
            self.thread.worktree if Path(self.thread.worktree).is_dir() else str(Path.cwd())
        )
        self.env_extra = {
            "AGENT_COMMS_THREAD": self.thread_name,
            "PI_AGENT_ID": self.thread_name,
            "AGENT_COMMS_ROOT": str(self.runner.comms.root),
            "PI_PARENT_ID": self.thread.parent or "",
            "AGENT_COMMS_MANAGED": "1",
            "PI_WORKTREE": self.worktree,
        }
        self.peers = [
            {key: person[key] for key in ("name", "status", "activity", "activity_detail")}
            for person in self.runner.comms.presence()
            if person["name"] != self.thread_name
        ][:50]
        self.task = (
            f"Coordination context: you are thread {self.thread_name!r}; "
            f"parent={self.thread.parent!r}. "
            "This identity overrides identities in inherited conversation history. "
            "Use your own identity for comms tools. Incoming direct messages automatically "
            "start a new turn when you are idle, or are delivered into your current turn. "
            "End your turn when done; never sleep or poll waiting for messages. "
            "Reply with comms_send when a reply is useful; otherwise call comms_dismiss "
            "with the channel to end quietly. Do not echo acknowledgments. "
            f"Your project directory is {self.thread.worktree!r}. Use comms_set_project(path) "
            "to change it persistently without creating another thread. After changing "
            "projects, end this turn; the runtime automatically resumes in the new project. "
            "When the user asks you to set or start a persistent goal, call "
            "comms_set_goal(text) so this same thread continues it autonomously. "
            f"Peer state: {json.dumps(self.peers)}\n\n{self.task}"
        )
        if self.goal is not None and self.goal.active:
            if self.direct_interrupt:
                self.task = (
                    f"Persistent goal {self.goal.id} is parked for this ordinary direct-message "
                    "interruption. This is NOT a goal attempt or declared dependency reply. "
                    "Respond to this message first; do not call comms_goal merely to finish "
                    "the DM, report goal progress, clear its standby wait, or retry an UNKNOWN "
                    "input. The goal remains separately scheduled. Current goal state for "
                    "answering questions about it only (verify live project state before "
                    "reporting current PR status):\n"
                    f"Goal status: {self.goal.status}; revision: {self.goal.revision}\n"
                    f"Objective: {self.goal.text}\nProgress: {self.goal.progress}\n\n" + self.task
                )
            else:
                self.task = (
                    f"Persistent goal {self.goal.id}: {self.goal.text}\n"
                    f"Progress: {self.goal.progress}\n"
                    "Work toward this goal while respecting follow-up instructions. "
                    "Use comms_goal with this goal_id to record useful progress. "
                    "Set status completed "
                    "only after verifying success, blocked when you need user input, or active "
                    "to continue useful work in another turn. When waiting for delegated work, "
                    "set status standby with explicit wait_for thread names "
                    "and explain what you need. "
                    "The goal stays active without polling; a direct message from a named "
                    "dependency "
                    "or an explicit user follow-up starts the next goal turn. "
                    "Do not return empty output "
                    "or repeatedly announce waiting. Do not wait or poll; "
                    "the owner schedules continuation.\n\n" + self.task
                )
        if self.thread.auto_title_pending:
            self.task = (
                "Give this new thread a concise topic title before doing the task: call "
                "comms_rename_self with a meaningful 2-5-word hyphenated name (at most 48 "
                "characters). Summarize the user's intent; do not copy their first message, "
                "greeting, or request phrasing. This is automatic naming for a new thread.\n\n"
                + self.task
            )
        if self.reply_targets:
            self.task = (
                f"Your final answer is delivered automatically to {', '.join(self.reply_targets)}. "
                "Do not use comms_send to duplicate that answer.\n\n" + self.task
            )

    def open_stream(self):
        self.backend_inbox: asyncio.Queue[str | dict[str, Any]] = asyncio.Queue()
        self.finish_event = asyncio.Event()
        self.controller = self.runner.runtime.controller.get()
        if self.controller is UNBOUND_CONTROLLER:
            self.controller = None  # Autonomous/channel/goal turns have no controller.
        self.runner.inputs.backend_inboxes[self.session_id] = self.backend_inbox
        self.runner.active_turns[self.session_id] = self.turn_id
        if self.original_owner_input and self.original_keys:
            self.runner.inputs.turn_original_input_keys[self.session_id] = tuple(self.original_keys)
            self.runner.inputs.turn_input_text[self.session_id] = self.original_display or self.task

    async def prepare_native(self):
        await self.runner.effects._emit_event(
            self.session_id,
            self.runner.started_event(self.thread_name, self.turn_id),
        )
        await self.runner.inputs.emit_input_delivery_changed(self.session_id)
        await self.runner.inputs.drain_inbox(self.session_id)

        # Existing local ACP owner session only. If delivery is uncertain,
        # the keyed metadata remains pending; never invent a bus recipient.
        await self.runner.effects.publish_pending_compaction(self.session_id, self.thread_name)
        # ACP delivery/ACK/UI updates above are not model context. This
        # bounded projection is prepared ONLY inside an already authorized
        # natural turn, from a separate owner-bound source cursor. It never
        # advances that cursor or creates a wake, claim or native receipt.
        if backend.rpc_args_for(self.runner.agent_bin, self.runner.agent_args) is not None:
            with _store_lock(self.runner.comms._wire_lock_path):
                self.snapshot = self.runner.comms.registry.snapshot()
                self.current_thread = self.snapshot.threads.get(self.thread_name)
                if (
                    self.current_thread is not None
                    and self.current_thread.created_at == self.thread.created_at
                    and self.snapshot.admission_generations.get(self.thread_name)
                    == self.turn_admission
                    and self.current_thread.active_turn is not None
                    and self.current_thread.active_turn.id == self.turn_id
                ):
                    try:
                        self.passive_frame = self.runner.inputs.passive_awareness.frame(
                            self.current_thread,
                            self.snapshot,
                            self.runner.comms.channel_catalog.targets_for(self.current_thread.tags),
                        )
                        if self.passive_frame:
                            self.passive_sources = self.runner.inputs.passive_awareness.sources(
                                self.current_thread
                            )
                            if self.passive_sources:
                                self.task += self.passive_frame
                            else:
                                self.passive_frame = ""
                    except (OSError, TypeError, ValueError):
                        # Before native start, omit the optional projection;
                        # the already-authorized owner task remains intact.
                        self.passive_frame = ""
                        self.passive_sources = ()
        if (
            self.runner.adaptive_compaction_enabled
            and self.original_owner_input
            and len(self.original_keys) == 1
            and self.thread.session_file is not None
            and backend.rpc_args_for(self.runner.agent_bin, self.runner.agent_args) is not None
        ):
            from .owner_compaction_adaptive import maybe_compact_owner_turn

            def admit_original(admission: SelectedSummaryAdmission) -> None:
                self.runner.inputs.selected_summary_admissions[self.session_id] = admission

            try:
                self.committed = await maybe_compact_owner_turn(
                    self.runner.comms.registry,
                    self.runner.agent_bin,
                    self.thread_name,
                    self.turn_id,
                    self.runner.comms.agent_info_of(self.thread_name),
                    self.original_keys[0],
                    self.runner.persistent_backends.setdefault(
                        self.session_id, backend.PersistentPiSession()
                    ),
                    summary_strategy=self.runner.adaptive_summary_strategy,
                    input_text=self.task,
                    on_admission=admit_original,
                    future_queue=self.runner.inputs,
                )
            except Exception:
                # A selected adaptive operation may already have paid or
                # written. Do not turn a fault into ordinary input fallback.
                if self.goal is not None and self.goal.active:
                    self.runner.comms.block_goal_after_failed_turn(
                        self.thread_name,
                        started_goal=self.goal,
                        expected_worktree=self.thread.worktree,
                        diagnostic=(
                            "Adaptive native compaction did not establish a "
                            "safe outcome; inspect the exact commit journal."
                        ),
                    )
                raise
            if self.committed:
                # Local metadata-only outbox; uncertain subscriber delivery
                # leaves its exact row pending, never broadcasts a summary.
                await self.runner.effects.publish_pending_compaction(
                    self.session_id, self.thread_name
                )
        self.session_file = self.thread.session_file
        self.fork_session = False
        if not self.session_file and self.thread.parent:
            self.session_file = self.runner.comms.registry.require(self.thread.parent).session_file
            self.fork_session = bool(self.session_file)
        self.image_options: dict[str, Any] = {"images": self.images} if self.images else {}

    async def stream(self):
        async for event in backend.stream_agent_events(
            self.runner.agent_bin,
            (
                backend.args_for_thinking_level(
                    backend.args_for_model(self.runner.agent_args, self.thread.model),
                    self.thread.thinking_level,
                )
                if backend.rpc_args_for(self.runner.agent_bin, self.runner.agent_args) is not None
                else self.runner.agent_args
            ),
            self.task,
            self.worktree,
            self.env_extra,
            **self.image_options,
            session_file=self.session_file,
            fork_session=self.fork_session,
            steering_queue=self.backend_inbox,
            finish_event=self.finish_event,
            send_boundary=self.send_boundary,
            interrupt_boundary=lambda public_id, native_id, text: self.send_boundary(
                public_id, native_id, text, already_bound=True
            ),
            native_start=self.native_start,
            persistent_session=(
                self.runner.persistent_backends.setdefault(
                    self.session_id, backend.PersistentPiSession()
                )
                if backend.rpc_args_for(self.runner.agent_bin, self.runner.agent_args) is not None
                else None
            ),
            ui_request=lambda request: self.runner.extension_ui_permission(
                self.session_id, self.turn_id, self.controller, request
            ),
        ):
            await self.progress.consume(event)

    async def finish(self):
        self.progress.finish_attempts()
        self.runner.emitted_errors.pop(self.session_id, None)
        self.thread_name = await self.runner.sessions.sync_identity(self.session_id)
        self.current_project = self.runner.comms.registry.require(self.thread_name).worktree
        if self.current_project != self.thread.worktree and (
            persistent := self.runner.persistent_backends.get(self.session_id)
        ):
            await persistent.close_idle()
        await self.runner.inputs.finish_turn_inputs(self.session_id, self.backend_inbox)
        if (
            not self.progress.cancelled
            and not self.runner.inputs.closing
            and self.current_project != self.thread.worktree
        ):
            self.runner.inputs.pending_turns.setdefault(self.session_id, []).append(
                ScheduledTurn(
                    "Project change completed: tools and context now use "
                    f"{self.current_project!r}. "
                    "Continue the user's previous request from this directory. "
                    "If the request was only to switch projects, report that you are ready; "
                    "do not invent extra work."
                )
            )
        await self.runner.settle_turn(
            self.session_id,
            self.thread_name,
            self.turn_id,
            self.turn_claim,
            stream_settled=self.progress.settled,
            terminal_fence=self.progress.terminal_fence,
        )

    async def run(self) -> None:
        if not self.admit():
            return
        self.begin()
        self.prepare_prompt()
        self.progress = TurnProgress(self)
        self.open_stream()
        try:
            await self.prepare_native()
            await self.stream()
            await self.progress.publish_result()
        except asyncio.CancelledError:
            self.progress.cancelled = True
            await backend.terminate_task_process(self.owner_task)
            raise
        except Exception as error:
            await self.progress.report_failure(error)
            await backend.terminate_task_process(self.owner_task)
            raise
        finally:
            await self.finish()


OwnedTurn.send_boundary._maintenance_wire_locked = True
