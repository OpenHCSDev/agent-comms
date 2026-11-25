"""Nominal goal semantics for external CLI harnesses bound to the bus.

External CLIs share bus-held goal storage and inbox wake delivery, while differing in
whether their harness supports hook-driven goal context injection or stop-hook turn
continuation. Capabilities compose through multiple inheritance onto ``ExternalGoalHarness``
so each bridge and hook declares its exact contract once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.comms import Comms
from agent_comms.messaging.sending import Messaging
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.registry.threads import Thread
from agent_comms.storage.store_files import AtomicFile
from agent_comms.task_attachments.owner_notices import GoalWakeNotice


@dataclass(frozen=True, slots=True)
class GoalCursorState:
    """The last bus goal state surfaced to an external CLI session."""

    thread: str = ""
    goal_id: str = ""
    revision: int = 0
    state: str = ""

    @classmethod
    def read(cls, path: Path) -> GoalCursorState:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        thread = raw.get("thread")
        goal_id = raw.get("goal_id")
        revision = raw.get("revision")
        state = raw.get("state")
        if (
            not isinstance(thread, str)
            or not isinstance(goal_id, str)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or not isinstance(state, str)
        ):
            return cls()
        return cls(thread=thread, goal_id=goal_id, revision=revision, state=state)

    def write(self, path: Path) -> None:
        AtomicFile(path).write_text(
            json.dumps(
                {
                    "thread": self.thread,
                    "goal_id": self.goal_id,
                    "revision": self.revision,
                    "state": self.state,
                },
                sort_keys=True,
            )
        )


class WakesViaBusGoal:
    """An external CLI woken through its bus inbox when its goal stays active."""

    @classmethod
    def wake_goal(cls, messaging: Messaging, thread: Thread) -> None:
        GoalWakeNotice.wake(messaging, thread)


class ContinuesActiveGoalOnStop:
    """An external CLI whose turn-end hook keeps an active bus goal moving."""

    idle_turns_asked: ClassVar[int] = 3

    @classmethod
    def supports_stop_continuation(cls) -> bool:
        return True

    @classmethod
    def continue_on_stop(
        cls,
        messaging: Messaging,
        thread: Thread,
        idle_turns: int,
    ) -> bool:
        """Wake ``thread`` while its bus goal is active and consecutive idle turns stay bounded."""
        held = GoalWakeNotice.reasons(thread)
        if held and idle_turns < cls.idle_turns_asked:
            GoalWakeNotice.send(messaging, thread, held)
            return True
        return False


class InjectsGoalContext:
    """An external CLI whose invocation hooks inject the bound thread's live bus goal."""

    @classmethod
    def supports_context_injection(cls) -> bool:
        return True

    @classmethod
    def goal_sync_guidance(cls, thread_name: str) -> str:
        return (
            f"Keep `{thread_name}`'s bus goal aligned with your actual work: call "
            "`comms_set_goal` when starting an objective, `comms_edit_goal` when scope "
            "shifts, `comms_goal` for progress/completed/blocked/standby, or "
            "`comms_clear_goal` when finished."
        )

    @classmethod
    def render_goal_context(
        cls,
        comms: Comms,
        thread: Thread,
    ) -> tuple[GoalCursorState, str]:
        snapshot = comms.goals.goal_snapshot(thread.name)
        for goal in snapshot.goal:
            todos = goal.order.todos(comms.goals, thread.incarnation, goal.id)
            text = goal.order.goal_text(goal, todos)
            progress = goal.order.progress(goal, todos)
            cursor = GoalCursorState(
                thread=thread.name,
                goal_id=str(goal.id),
                revision=goal.revision,
                state=goal.state.declared_name,
            )
            lines = [
                f"Bus goal for `{thread.name}` ({goal.id}, rev {goal.revision}, "
                f"{snapshot.execution.label}): {text}",
            ]
            if progress:
                lines.append(f"Progress: {progress}")
            lines.append(cls.goal_sync_guidance(thread.name))
            return cursor, "\n".join(lines)
        cursor = GoalCursorState(thread=thread.name, goal_id="", revision=0, state="none")
        return cursor, f"No bus goal set on `{thread.name}`. {cls.goal_sync_guidance(thread.name)}"


class ExternalGoalHarness(DeclaredFamily, affix="GoalHarness"):
    """Nominal goal-semantics contract for an external CLI harness on the bus."""

    @classmethod
    def supports_stop_continuation(cls) -> bool:
        return False

    @classmethod
    def supports_context_injection(cls) -> bool:
        return False


class InboxGoalHarness(WakesViaBusGoal, ExternalGoalHarness):
    """Generic external CLI/MCP session driven through inbox watches and goal tools."""


class CodexGoalHarness(WakesViaBusGoal, ExternalGoalHarness):
    """Codex CLI session driven by CodexBridge inbox injection and goal tools."""


class ClaudeGoalHarness(
    ContinuesActiveGoalOnStop,
    InjectsGoalContext,
    WakesViaBusGoal,
    ExternalGoalHarness,
):
    """Claude Code session with Stop-hook goal continuation and hook goal-context injection."""


class AntigravityGoalHarness(
    ContinuesActiveGoalOnStop,
    InjectsGoalContext,
    WakesViaBusGoal,
    ExternalGoalHarness,
):
    """Antigravity session bridging PreInvocation/AfterAgent hooks and `/goal` to the bus."""

    @classmethod
    def goal_sync_guidance(cls, thread_name: str) -> str:
        return (
            f"Keep `{thread_name}`'s bus goal aligned with your actual work (including any "
            "`/goal` run): call `comms_set_goal` when starting an objective, `comms_edit_goal` "
            "when scope shifts, `comms_goal` for progress/completed/blocked/standby, or "
            "`comms_clear_goal` when finished."
        )
