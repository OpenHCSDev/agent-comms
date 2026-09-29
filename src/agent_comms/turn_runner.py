"""Persistent ACP turn orchestration over existing session/input and S2 owners."""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp.schema import (
    PromptResponse,
    RequestPermissionResponse,
)

from . import agent_events as events
from . import backend
from . import pi_events as pi
from .acp_failure import ACPFailure
from .channel_targets import BuiltinChannel
from .comms import Comms
from .errors import RelationViolationError
from .goal_actions import (
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
)
from .goal_scheduler import GoalScheduler
from .input_drain import InputDrain
from .messages import Message
from .native_arguments import NativeArguments
from .runtime import (
    ACP_PERMISSION_TIMEOUT_SECONDS,
    RuntimeServer,
    SocketClient,
)
from .runtime_info import AgentRuntimeInfo
from .session_lifecycle import SessionLifecycle
from .store_files import _store_lock
from .threads import Thread
from .transcript_updates import StartedTranscriptUpdate
from .turn_effects import TurnEffects
from .turn_lease import FinishedTurnFence, TurnLeaseFence

if TYPE_CHECKING:
    from .coordinated_runtime import SelectedExecution


AGENT_PREFIX = "!agent "
RELAY_PREFIX = "!relay "
DEFAULT_AGENT_BIN = "pi"
DEFAULT_AGENT_ARGS = [
    "--provider",
    "openrouter",
    "--model",
    "z-ai/glm-5.3-flash",
]
NO_REPLY_WINDOW = 2.5  # silence: end the turn after this long with nothing
REPLY_WINDOW = 8.0  # once replies flow, keep collecting at most this long
REPLY_QUIET = 1.5  # after the last reply, wait this long then end the turn
REPLY_POLL = 0.25
ACTIVITY_WINDOW = 60.0  # keep the turn open while a peer is thinking/working
IDLE_GRACE = 1.0  # peers idle for this long -> drain and end the turn


class TurnRunner:
    """Own session turn locks/tasks and persistent S2 children."""

    def __init__(
        self,
        comms: Comms,
        runtime: RuntimeServer,
        effects: TurnEffects,
        *,
        agent_bin: str | None,
        agent_args: list[str] | None,
        adaptive_compaction_enabled: bool,
        adaptive_summary_strategy: Any,
        reply_window: float | None,
        no_reply_window: float | None,
        reply_quiet: float | None,
    ):
        self.comms = comms
        self.runtime = runtime
        self.effects = effects
        self.adaptive_compaction_enabled = adaptive_compaction_enabled
        self.adaptive_summary_strategy = adaptive_summary_strategy
        self.agent_bin = agent_bin or os.environ.get("AGENT_COMMS_AGENT_BIN", DEFAULT_AGENT_BIN)
        arg_env = os.environ.get("AGENT_COMMS_AGENT_ARGS")
        self.agent_args = NativeArguments.parse(
            agent_args
            if agent_args is not None
            else (shlex.split(arg_env) if arg_env is not None else list(DEFAULT_AGENT_ARGS))
        )
        self.turn_tasks: dict[str, asyncio.Task[Any]] = {}
        self.persistent_backends: dict[str, backend.PersistentPiSession] = {}
        self.turn_locks: dict[str, asyncio.Lock] = {}
        self.active_turns: dict[str, str] = {}
        self.emitted_errors: dict[str, ACPFailure] = {}
        self.goals = GoalScheduler(comms, effects, self.session_busy)
        self.reply_window = (
            reply_window
            if reply_window is not None
            else float(os.environ.get("AGENT_COMMS_REPLY_WINDOW", str(REPLY_WINDOW)))
        )
        self.no_reply_window = (
            no_reply_window
            if no_reply_window is not None
            else float(os.environ.get("AGENT_COMMS_NO_REPLY_WINDOW", str(NO_REPLY_WINDOW)))
        )
        self.reply_quiet = (
            reply_quiet
            if reply_quiet is not None
            else float(os.environ.get("AGENT_COMMS_REPLY_QUIET", str(REPLY_QUIET)))
        )

    def bind(self, sessions: SessionLifecycle, inputs: InputDrain) -> None:
        self.sessions = sessions
        self.inputs = inputs
        self.goals.bind(sessions, inputs)

    def session_busy(self, session_id: str) -> bool:
        return session_id in self.turn_tasks or session_id in self.active_turns

    def native_arguments(self, thread: Thread) -> tuple[str, ...]:
        return self.agent_args.with_model(thread.model).with_thinking(thread.thinking_level).argv

    def native_environment(self, thread: Thread, worktree: str) -> dict[str, str]:
        return {
            "AGENT_COMMS_THREAD": thread.name,
            "PI_AGENT_ID": thread.name,
            "AGENT_COMMS_ROOT": str(self.comms.root),
            "PI_PARENT_ID": thread.parent or "",
            "AGENT_COMMS_MANAGED": "1",
            "PI_WORKTREE": worktree,
        }

    async def prepare_selected_session(self, session_id: str, thread: Thread) -> AgentRuntimeInfo:
        from .native_session_prepare import NativeSessionPreparation

        if thread.session_file is None:
            raise ValueError("Native preparation requires a saved session")
        state = await NativeSessionPreparation.open(
            self.persistent_backends.setdefault(session_id, backend.PersistentPiSession()),
            self.agent_bin,
            self.native_arguments(thread),
            worktree=thread.worktree,
            environment=self.native_environment(thread, thread.worktree),
            session_file=thread.session_file,
        )
        if state.model is None or state.model.display_name != thread.model:
            raise ValueError("Prepared native model does not match the owner selection")
        return AgentRuntimeInfo(
            thread=thread.name,
            model=state.model.display_name,
            session_name=state.session_name,
            context_size=state.model.context_window,
        )

    async def prompt_owned(
        self, session_id: str, prompt: list[Any], *, display_text: str | None = None
    ) -> PromptResponse:
        turn_task = asyncio.current_task()
        assert turn_task is not None
        self.turn_tasks[session_id] = turn_task
        try:
            thread_name = await self.sessions.sync_identity(session_id)
            self.comms.registry.require(thread_name)
            text = self.effects._prompt_text(prompt)
            images = self.effects._prompt_images(prompt)
            agent_task: str | None = None
            relay_text: str | None = None
            if text.startswith(AGENT_PREFIX):
                agent_task = text[len(AGENT_PREFIX) :].strip()
            elif text.startswith(RELAY_PREFIX):
                relay_text = text[len(RELAY_PREFIX) :].strip()
            elif text.lstrip().startswith(("@", "#")):
                relay_text = text
            else:
                agent_task = text.strip()
            sent_seq = 0
            if relay_text:
                target, body = parse_target(relay_text)
                self.comms.messaging.send(thread_name, target, body)
                sent_seq = self.comms.bus.log.total_messages()
            self.effects._debug_log(
                f"prompt:start sender={thread_name} mode={'agent' if agent_task else 'relay'} "
                f"sent_seq={sent_seq}"
            )
            if images:
                await self.inputs.run_owned_input(
                    session_id,
                    thread_name,
                    agent_task or "",
                    images=images,
                    display_text=display_text,
                )
            elif agent_task:
                await self.inputs.run_owned_input(
                    session_id, thread_name, agent_task, display_text=display_text
                )
            else:
                turn_id = uuid4().hex
                turn_lease = self.comms.agents.begin_turn(
                    thread_name, turn_id, "Waiting for replies"
                )
                self.active_turns[session_id] = turn_id
                try:
                    await self.effects._emit_event(
                        session_id, self.started_event(thread_name, turn_id)
                    )
                    await self.inputs.drain_inbox(session_id)
                    await self.collect_replies(session_id, thread_name, sent_seq)
                finally:
                    await self.settle_turn(session_id, thread_name, turn_id, turn_lease)
            self.effects._debug_log("prompt:returning")
            return PromptResponse(stop_reason="end_turn")
        except asyncio.CancelledError:
            self.effects._debug_log("prompt:cancelled")
            await backend.terminate_task_process(turn_task)
            return PromptResponse(stop_reason="cancelled")
        finally:
            if self.turn_tasks.get(session_id) is turn_task:
                self.turn_tasks.pop(session_id, None)
            self.inputs.ensure_live_drain(session_id)

    async def collect_replies(self, session_id: str, thread_name: str, sent_seq: int = 0) -> None:
        """Stream inbox messages into the open turn until quiet or timeout.

        While a peer is on it (activity thinking/working, or a peer's read
        marker past our message), the turn stays open — the client shows
        its spinner, and replies drain the moment they land.
        """
        waited = 0.0
        quiet = 0.0
        got_reply = False
        idle_for = 0.0
        while True:
            await asyncio.sleep(REPLY_POLL)
            waited += REPLY_POLL
            quiet += REPLY_POLL
            pushed = await self.inputs.drain_count(session_id)
            if pushed:
                got_reply = True
                quiet = 0.0
                idle_for = 0.0
            if got_reply:
                if quiet >= self.reply_quiet or waited >= self.reply_window:
                    self.effects._debug_log(f"collect:break got_reply waited={waited}")
                    break
                continue
            # No reply yet: keep waiting while a peer is on it.
            peer_progress = self.peer_progress(thread_name, sent_seq)
            idle_for = 0.0 if peer_progress else idle_for + REPLY_POLL
            if idle_for >= IDLE_GRACE and waited >= self.no_reply_window:
                break
            if waited >= ACTIVITY_WINDOW:
                break

    def peer_progress(self, thread_name: str, sent_seq: int) -> bool:
        """True when a peer is active on, or has read, our message."""
        for name, activity in self.comms.agents.all_activity().items():
            if name != thread_name and activity.state.busy:
                return True
        if sent_seq:
            snapshot = self.comms.registry.snapshot()
            document = self.comms.bus.reads.read()
            for name in snapshot.threads:
                if name != thread_name and sent_seq in self.comms.bus.reads.seen_sequences(
                    name, snapshot, document=document
                ):
                    return True
        return False

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        if session_id in self.sessions.proxies:
            await self.sessions.proxies[session_id].request("cancel")
            return
        name = self.sessions.bindings.get(session_id)
        if name and (goal := self.comms.registry.require(name).goal) and goal.state.active:
            self.comms.goals.update_goal(
                name,
                PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
                actor=OwnerInvocable,
            )
        task = self.turn_tasks.get(session_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await backend.terminate_task_process(task)
        thread_name = self.sessions.bindings.get(session_id)
        if thread_name:
            self.comms.messaging.acknowledge(thread_name)

    async def extension_ui_permission(
        self,
        session_id: str,
        turn_id: str,
        controller: Any,
        request: pi.DialogUiRequest,
    ) -> pi.ExtensionUiChoice:
        """Project one bounded Pi UI dialog to exactly the turn's ACP controller.

        No ACP response updates package configuration, launch trust or call grants.
        The backend revalidates this result before replying to the same Pi child.
        """
        if self.active_turns.get(session_id) != turn_id or controller is None:
            return pi.CancelledUiChoice()
        permission = request.permission(turn_id)
        if permission is None:
            return pi.CancelledUiChoice()
        tool_call, options = permission
        try:
            if isinstance(controller, SocketClient):
                reply = await self.runtime.request_permission(
                    session_id,
                    controller,
                    {
                        "toolCall": tool_call.model_dump(by_alias=True, exclude_none=True),
                        "options": [
                            option.model_dump(by_alias=True, exclude_none=True)
                            for option in options
                        ],
                    },
                )
                if not isinstance(reply, dict):
                    return pi.CancelledUiChoice()
                outcome = reply
            elif controller is self.sessions.client:
                response = await asyncio.wait_for(
                    controller.request_permission(
                        session_id=session_id, tool_call=tool_call, options=options
                    ),
                    timeout=ACP_PERMISSION_TIMEOUT_SECONDS,
                )
                outcome = RequestPermissionResponse.model_validate(response).outcome.model_dump(
                    by_alias=True, exclude_none=True
                )
            else:
                return pi.CancelledUiChoice()
        except Exception:
            # An ACP controller exception is denial, never a raw error in Pi
            # RPC/model output or a reason to resend an uncertain MCP call.
            return pi.CancelledUiChoice()
        if self.active_turns.get(session_id) != turn_id:
            return pi.CancelledUiChoice()
        if isinstance(controller, SocketClient) and not self.runtime.is_controller(
            session_id, controller
        ):
            return pi.CancelledUiChoice()
        selected = outcome.get("optionId")
        if outcome.get("outcome") != "selected" or type(selected) is not str:
            return pi.CancelledUiChoice()
        return request.choice(selected)

    def finish_turn_stream(
        self,
        session_id: str,
        thread_name: str,
        turn_id: str,
        lease: TurnLeaseFence,
    ) -> FinishedTurnFence | None:
        """Clear only this turn; waiter release follows committed terminal output."""
        fence = self.comms.agents.finish_turn(lease)
        if self.active_turns.get(session_id) == turn_id:
            self.active_turns.pop(session_id, None)
        return fence

    async def settle_turn(
        self,
        session_id: str,
        thread_name: str,
        turn_id: str,
        lease: TurnLeaseFence,
        *,
        stream_settled: bool = False,
        terminal_fence: FinishedTurnFence | None = None,
        task: asyncio.Task[Any] | None = None,
    ) -> None:
        """Settle after terminal publication, releasing waiters even if the UI fails.

        Native StreamSettled has already finished and published the stream. Its
        fence is retained until Done and relay output are committed. Manual
        compaction and relay turns take both phases here.
        """
        if task is not None and self.turn_tasks.get(session_id) is task:
            self.turn_tasks.pop(session_id, None)
        if not stream_settled:
            terminal_fence = self.finish_turn_stream(session_id, thread_name, turn_id, lease)
        try:
            if not stream_settled:
                await self.effects._emit_event(session_id, events.TurnSettled(turn_id))
        finally:
            self.comms.goals.release_waits_after_terminal_turn(terminal_fence)

    def started_event(self, thread_name: str, turn_id: str) -> StartedTranscriptUpdate:
        """Project one owner-authored turn without inventing presentation timestamps."""
        thread = self.comms.registry.require(thread_name)
        active = thread.active_turn
        activity = self.comms.agents.activity_of(thread.name)
        return StartedTranscriptUpdate(
            turn_id=turn_id,
            started_at=active.started_at if active is not None and active.id == turn_id else None,
            activity=activity.state.value,
            activity_detail=activity.detail,
        )

    async def replay_turn_state(self, session_id: str, client: Any = None) -> None:
        thread_name = self.sessions.require(session_id)
        active = self.comms.registry.require(thread_name).active_turn
        event = (
            self.started_event(thread_name, active.id)
            if active is not None
            else events.NoActiveTurn()
        )
        await self.effects._emit_event(session_id, event, client=client)

    def active_backend_inbox(self, session_id: str) -> asyncio.Queue | None:
        return (
            self.inputs.backend_inboxes.get(session_id) if session_id in self.active_turns else None
        )

    async def close_idle_backend(self, session_id: str) -> None:
        if session_id not in self.active_turns and (
            persistent := self.persistent_backends.get(session_id)
        ):
            await persistent.close_idle()

    async def run_selected(self, session_id: str, execution: SelectedExecution):
        """Selected work and fresh ACP input share this session's one turn lifetime.

        Only the live inbox transfers to the next native turn. Durable UNKNOWN
        rows never create input, and selected failure/cancellation retires the
        inbox without pretending that its pending prompts reached the model.
        """
        async with self.turn_locks.setdefault(session_id, asyncio.Lock()):
            task = asyncio.current_task()
            assert task is not None
            inbox: asyncio.Queue[str | dict[str, Any]] = asyncio.Queue()
            self.turn_tasks[session_id] = task
            self.inputs.backend_inboxes[session_id] = inbox
            try:
                result = await execution.run()
                while not inbox.empty():
                    command = inbox.get_nowait()
                    if not isinstance(command, dict) or not (input_id := command.get("_input_id")):
                        continue  # No accepted prompt: clear/interrupt controls carry no input.
                    item = self.inputs.queued_inputs.get(session_id, {}).get(input_id)
                    key = self.inputs.steering_input_keys.get(session_id, {}).get(input_id)
                    if item is None:
                        await self.inputs.input_refused(session_id, input_id)
                        continue
                    try:
                        with _store_lock(self.comms._wire_lock_path):
                            snapshot = self.comms.registry.snapshot()
                            name = snapshot.aliases.get(execution.owner_name, execution.owner_name)
                            row = self.inputs.dispositions.read().lookup(key)
                            item.require_handoff(
                                snapshot, name, self.comms.goals.goal_wait(name), row, input_id,
                            )
                    except RelationViolationError:
                        await self.inputs.input_refused(session_id, input_id)
                        continue
                    await self.run_agent_turn(
                        session_id,
                        name,
                        command["message"],
                        images=item.images,
                        original_keys=(key,),
                        initial_display_text=item.text if item.echo else None,
                        original_owner_input=True,
                        original_goal_id=item.context.active_goal_id,
                        accepted_input_id=input_id,
                    )
                    break  # The existing native forwarder consumes the remaining live inbox.
                return result
            finally:
                await self.inputs.finish_turn_inputs(session_id, inbox)
                if self.turn_tasks.get(session_id) is task:
                    self.turn_tasks.pop(session_id, None)

    async def run_agent_turn(
        self,
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
        accepted_input_id: str | None = None,
        dependency_wait_id: str | None = None,
    ) -> None:
        from .owned_turn import OwnedTurn

        await OwnedTurn(
            self,
            session_id=session_id,
            thread_name=thread_name,
            task=task,
            reply_targets=reply_targets,
            origins=origins,
            images=images,
            original_keys=original_keys,
            initial_display_text=initial_display_text,
            autonomous_goal=autonomous_goal,
            original_owner_input=original_owner_input,
            original_goal_id=original_goal_id,
            accepted_input_id=accepted_input_id,
            dependency_wait_id=dependency_wait_id,
        ).run()

    async def close(self) -> None:
        tasks = tuple(self.turn_tasks.values())
        self.turn_tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            *(backend.terminate_task_process(task) for task in tasks), return_exceptions=True
        )
        for persistent in self.persistent_backends.values():
            await persistent.close_idle()
        self.persistent_backends.clear()


def parse_target(text: str) -> tuple[str, str]:
    """Split a leading target prefix off a prompt.

    ``@name body`` -> DM; ``#channel body`` -> channel; empty body with a
    bare target composes nothing; otherwise the global channel. The prefix
    is consumed, never broadcast.
    """
    stripped = text.strip()
    if stripped.startswith("@") and len(stripped) > 1:
        parts = stripped[1:].split(maxsplit=1)
        name = parts[0].rstrip("@#")
        if name and len(parts) == 2 and parts[1].strip():
            return name, parts[1].strip()
    if stripped.startswith("#") and len(stripped) > 1:
        parts = stripped[1:].split(maxsplit=1)
        channel = parts[0].rstrip("@#")
        if channel and len(parts) == 2 and parts[1].strip():
            return f"#{channel}", parts[1].strip()
    return BuiltinChannel.ALL.value, stripped
