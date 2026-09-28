"""Goal commands own behavior; one parent owns CAS, publication and actor checks."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from .command import Command
from .declarations import Goal, Thread
from .declared_family import DeclaredFamily
from .goal_mentions import bind_goal_mentions
from .goal_pauses import GoalPauseEvent, GoalPauseEvents
from .goal_states import (
    ActiveGoal,
    BlockedGoal,
    CompletedGoal,
    GoalState,
    OwnerPause,
    PausedGoal,
    RuntimePause,
)
from .goal_waits import GoalWait, GoalWaits

if TYPE_CHECKING:
    from .goal_attempts import GoalAttemptStore
    from .operations import Comms


class ModelInvocable:
    pass


class OwnerInvocable:
    pass


class RuntimeInvocable:
    pass


class OwnerControlInvocable:
    pass


@dataclass(frozen=True)
class GoalPrecondition:
    goal_id: str | None = None
    expected_status: str | None = None
    expected_goal: Goal | None = None
    expected_owner_pid: int | None = None

    def check(self, ctx: GoalActionContext) -> None:
        thread, goal = ctx.thread, ctx.thread.goal
        if self.expected_owner_pid is not None and (
            thread.pid != self.expected_owner_pid
            or not ctx.comms.registry.status(thread.name).running
        ):
            raise ValueError("The goal owner changed; refresh its state.")
        if self.expected_goal is not None and goal != self.expected_goal:
            raise ValueError("Goal changed during resume; refresh its state.")
        if self.goal_id is not None and (goal is None or goal.id != self.goal_id):
            raise ValueError("This goal was replaced or cleared; refresh its state.")
        if self.expected_status is not None and (
            goal is None or goal.status != self.expected_status
        ):
            pause = goal.state.pause_source if goal is not None else None
            raise ValueError(
                (pause.instruction() if pause else None)
                or "This goal is no longer active; refresh its state."
            )


@dataclass(frozen=True)
class GoalActionContext:
    comms: Comms
    thread: Thread
    actor: type = RuntimeInvocable
    owner_store: GoalAttemptStore | None = None

    @property
    def report_turn(self) -> str:
        return self.thread.active_turn.id if self.thread.active_turn is not None else ""

    @property
    def model_report(self) -> bool:
        return self.actor is ModelInvocable

    def require_goal(self) -> Goal:
        if self.thread.goal is None:
            raise ValueError("No goal is set for this thread.")
        return self.thread.goal


@dataclass(frozen=True, kw_only=True)
class GoalAction(DeclaredFamily, Command, affix="GoalAction"):
    expect: GoalPrecondition = GoalPrecondition()
    preserve_wait: ClassVar[bool] = False
    owner_grant: ClassVar[bool] = False
    schedules_goal: ClassVar[bool] = False

    @classmethod
    def model_choices(cls) -> tuple[str, ...]:
        return tuple(member.declared_name for member in cls.members_with(ModelInvocable))


    def check_grant(self, ctx: GoalActionContext) -> None:
        if ctx.owner_store is not None:
            raise ValueError("Owner goal authority requires goal creation or explicit resume.")

    def apply(self, ctx: GoalActionContext) -> Goal | None:
        self.expect.check(ctx)
        if not isinstance(self, ctx.actor):
            raise ValueError(f"This actor cannot take goal action {self.declared_name!r}.")
        self.check_grant(ctx)
        if ctx.model_report and ctx.thread.last_goal_report_turn == ctx.report_turn:
            raise ValueError("This goal was already reported in this turn.")
        goal = self.change(ctx)
        self.before_publish(goal, ctx)
        ctx.comms.registry.register(
            replace(
                ctx.thread,
                goal=goal,
                last_goal_report_turn=(
                    ctx.report_turn if ctx.model_report else ctx.thread.last_goal_report_turn
                ),
            ),
            ctx.comms.registry.status(ctx.thread.name),
        )
        if not self.preserve_wait and ctx.thread.goal is not None:
            GoalWaits(ctx.comms.root / GoalWaits.filename).clear(ctx.thread.goal.id)
        if goal is not None and goal.state.pause_source is not None:
            # Audit only; current pause authority is already durable in Goal.
            GoalPauseEvents(ctx.comms.root / GoalPauseEvents.filename).record(
                GoalPauseEvent(goal.id, goal.revision, goal.state.pause_source.declared_name)
            )
        return goal

    @abstractmethod
    def change(self, ctx: GoalActionContext) -> Goal | None: ...

    def before_publish(self, goal: Goal | None, ctx: GoalActionContext) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class TransitionGoalAction(GoalAction):
    progress: str | None = None

    @abstractmethod
    def next_state(self, ctx: GoalActionContext) -> GoalState: ...

    def change(self, ctx: GoalActionContext) -> Goal:
        goal = ctx.require_goal()
        state = self.next_state(ctx)
        goal.state.check_transition(state, owner=ctx.actor is OwnerInvocable)
        self.before_transition(ctx)
        return replace(
            goal,
            state=state,
            revision=goal.revision + 1,
            progress=goal.progress if self.progress is None else self.progress,
            reported_turn=ctx.report_turn if ctx.model_report else goal.reported_turn,
        )

    def before_transition(self, ctx: GoalActionContext) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class ActiveGoalAction(
    TransitionGoalAction, ModelInvocable, OwnerInvocable, RuntimeInvocable, OwnerControlInvocable
):
    owner_grant = True
    schedules_goal = True

    def check_grant(self, ctx: GoalActionContext) -> None:
        if ctx.owner_store is not None and (
            ctx.actor is not OwnerInvocable or self.expect.expected_owner_pid is None
        ):
            super().check_grant(ctx)

    def next_state(self, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def before_transition(self, ctx: GoalActionContext) -> None:
        goal, thread = ctx.require_goal(), ctx.thread
        if ctx.owner_store is not None:
            generation = ctx.owner_store.snapshot(goal.id)
            if generation is None:
                raise ValueError("Goal launch authority is missing; inspect it before Retry.")
            if generation.lifecycle.failed and generation.attempt_id:
                # A failed/uncertain attempt needs the explicit Retry
                # decision, not a status-only Resume. Expose that state
                # immediately so the UI offers the correct control.
                # Persist the bounded refusal explanation so a reload
                # never shows 'reason unavailable' on a fresh row.
                refusal = required_block_reason(
                    "The interrupted goal attempt is unresolved. Inspect it, then use "
                    "Retry to authorize a new attempt. Your messages can still be sent."
                )
                blocked = replace(
                    goal,
                    state=BlockedGoal(refusal),
                    progress=goal.progress,
                    revision=goal.revision + 1,
                )
                ctx.comms.registry.register(
                    replace(thread, goal=blocked), ctx.comms.registry.status(thread.name)
                )
                raise ValueError(refusal)
            elif not generation.lifecycle.allows_resume(thread.active_turn is not None):
                raise ValueError("The goal attempt is unresolved; inspect it before Retry.")


@dataclass(frozen=True, kw_only=True)
class StandbyGoalAction(TransitionGoalAction, ModelInvocable, RuntimeInvocable):
    wait_for: tuple[str, ...] = ()
    reviewed_inputs: tuple[str, ...] = ()
    preserve_wait = True

    def next_state(self, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def before_publish(self, goal: Goal | None, ctx: GoalActionContext) -> None:
        assert goal is not None
        thread = ctx.thread
        report_turn = ctx.report_turn
        review = ctx.comms._goal_input_review(thread, goal.id, self.wait_for)
        wait_targets = review.targets
        from .input_disposition import AcpDeliveryCursors, InputDispositions

        aliases = review.owners
        cursor = AcpDeliveryCursors(ctx.comms.root).cursor(aliases)
        dispositions = InputDispositions(ctx.comms.root)
        unknown = {row["key"]: row for row in review.unknown}
        reviewed_keys = tuple(dict.fromkeys(self.reviewed_inputs))
        if any(key not in unknown or unknown[key]["sequence"] is None for key in reviewed_keys):
            raise ValueError("Review only this recipient's exact unresolved bus input keys.")
        reviewed_sequences = {unknown[key]["sequence"] for key in reviewed_keys}
        prior_reviews = {
            row["sequence"]
            for row in unknown.values()
            if goal is not None and dispositions.reviewed_for_goal(row, goal.id)
        }
        unresolved = {row["sequence"] for row in unknown.values() if row["sequence"] is not None}
        senders = review.senders
        if not set(reviewed_keys) <= review.eligible_keys:
            raise ValueError("Review only direct inputs from these declared dependencies.")
        pending = ctx.comms.bus._history_page(
            lambda message: message.target in aliases
            and message.sender in senders
            and (message.seq > cursor or message.seq in unresolved)
            and message.seq not in reviewed_sequences | prior_reviews,
            before=None,
            after=None,
            limit=1,
            max_bytes=256 * 1024,
        )
        if pending.messages:
            sequence = pending.messages[0].seq
            raise ValueError(
                f"Dependency reply {sequence} is already pending or UNKNOWN. "
                f"Call comms_inbox with goal_id={goal.id!r} and "
                f"wait_for={list(self.wait_for)!r}. Inspect standby_review.messages, then "
                "pass only standby_review.reviewed_inputs to comms_goal to wait "
                "for a later reply. Do not pass excluded owner or other dependency inputs. "
                "This does not mark them STARTED or replay them."
            )
        snapshot = ctx.comms.registry.snapshot()
        if not any(
            GoalWaits.target_has_active_turn(target, snapshot)
            and ctx.comms._process_alive(
                snapshot.threads[snapshot.aliases.get(target.name, target.name)].pid
            )
            for target in wait_targets
        ):
            names = ", ".join(f"@{target.name}" for target in wait_targets)
            raise ValueError(
                f"No declared dependency has an active turn ({names}). "
                "A running/ready process or queued input does not prove active work. "
                "Message or restart the responsible agent, inspect its status, "
                "then declare standby only while a target is actually working."
            )
        closed = GoalWaits.closed_wait_group(
            thread.name,
            wait_targets,
            GoalWaits(ctx.comms.root / GoalWaits.filename).snapshot(),
            snapshot,
            ctx.comms._process_alive,
        )
        if closed:
            names = ", ".join(f"@{name}" for name in closed)
            raise ValueError(
                f"Standby would close a dependency wait group ({names}). "
                "At least one agent must remain able to work or reply. "
                "Continue independent work or change the dependencies."
            )
        dispositions.review_for_goal(
            reviewed_keys,
            owners=aliases,
            goal_id=goal.id,
            goal_revision=goal.revision,
            turn_id=report_turn,
        )
        # Commit scheduling authority first. A crash before the registry
        # progress update must leave this same goal waiting, not runnable.
        GoalWaits(ctx.comms.root / GoalWaits.filename).record(
            GoalWait(
                goal.id,
                uuid4().hex,
                goal.revision,
                ctx.comms.message_high_water(),
                wait_targets,
                owner_created_at=thread.created_at,
                report_turn_id=thread.active_turn.id if thread.active_turn else None,
                report_turn_generation=(thread.turn_generation if thread.active_turn else None),
                target_turn_generations=tuple(
                    (
                        snapshot.threads[
                            snapshot.aliases.get(target.name, target.name)
                        ].turn_generation
                        if GoalWaits.target_has_active_turn(target, snapshot)
                        else None
                    )
                    for target in wait_targets
                ),
            )
        )


@dataclass(frozen=True, kw_only=True)
class CompletedGoalAction(TransitionGoalAction, ModelInvocable, OwnerInvocable, RuntimeInvocable):
    def next_state(self, ctx: GoalActionContext) -> GoalState:
        return CompletedGoal()


@dataclass(frozen=True, kw_only=True)
class BlockedGoalAction(TransitionGoalAction, ModelInvocable, OwnerInvocable, RuntimeInvocable):
    block_reason: str | None = None

    def next_state(self, ctx: GoalActionContext) -> GoalState:
        return BlockedGoal(
            required_block_reason(
                self.block_reason if self.block_reason is not None else self.progress
            )
        )


@dataclass(frozen=True, kw_only=True)
class PausedGoalAction(
    TransitionGoalAction, OwnerInvocable, RuntimeInvocable, OwnerControlInvocable
):
    def next_state(self, ctx: GoalActionContext) -> GoalState:
        current = ctx.require_goal().state.pause_source
        return PausedGoal(
            current or (OwnerPause() if ctx.actor is OwnerInvocable else RuntimePause())
        )


@dataclass(frozen=True, kw_only=True)
class ReplacementGoalAction(GoalAction):
    def before_publish(self, goal: Goal | None, ctx: GoalActionContext) -> None:
        goal = ctx.thread.goal
        if goal is not None:
            # Revoke a protected goal before removing or replacing its
            # registry identity. If the registry write then fails, the
            # remaining visible goal is safely unlaunchable.
            from .goal_attempts import GoalAttemptStore

            private = ctx.comms.root / "goal-private"
            if (private / "goal_attempts.sqlite3").exists():
                attempts = GoalAttemptStore(private)
                generation = attempts.snapshot(goal.id)
                if generation is not None and not generation.lifecycle.terminal:
                    attempts.retire_goal(
                        goal.id,
                        expected_generation=generation.number,
                        attempt_id=generation.attempt_id,
                    )


@dataclass(frozen=True, kw_only=True)
class SetGoalAction(ReplacementGoalAction, OwnerInvocable, RuntimeInvocable):
    text: str

    def check_grant(self, ctx: GoalActionContext) -> None:
        pass

    def change(self, ctx: GoalActionContext) -> Goal:
        goal = Goal(text=self.text.strip(), id=uuid4().hex, revision=1)
        goal = replace(
            goal,
            mention_source=bind_goal_mentions(
                goal.text, goal.id, goal.revision, ctx.thread, ctx.comms.registry.snapshot()
            ),
        )
        if ctx.owner_store is not None:
            ctx.owner_store.create_goal(goal.id)
        return goal


@dataclass(frozen=True, kw_only=True)
class ClearGoalAction(
    ReplacementGoalAction, OwnerInvocable, RuntimeInvocable, OwnerControlInvocable
):
    def change(self, ctx: GoalActionContext) -> None:
        return None


@dataclass(frozen=True, kw_only=True)
class EditGoalAction(GoalAction, OwnerInvocable, RuntimeInvocable):
    text: str
    preserve_wait = True

    def change(self, ctx: GoalActionContext) -> Goal:
        goal = ctx.require_goal()
        text = self.text.strip()
        if not text:
            raise ValueError("A goal requires text.")
        revision = goal.revision + 1
        return replace(
            goal,
            text=text,
            revision=revision,
            mention_source=bind_goal_mentions(
                text, goal.id, revision, ctx.thread, ctx.comms.registry.snapshot()
            ),
        )


def required_block_reason(reason: str | None) -> str:
    """Validate a new block's own reason; prior progress is never a fallback."""
    if type(reason) is not str or not reason.strip():
        raise ValueError("Blocking a goal requires a nonempty reason for the needed input.")
    normalized = reason.strip()
    if len(normalized) > 1024:
        raise ValueError("A blocked-goal reason must be at most 1024 characters.")
    return normalized


@dataclass(frozen=True, kw_only=True)
class RetryGoalAction(GoalAction, OwnerInvocable):
    """Explicit owner retry; existing store grants still fence every attempt."""

    def check_grant(self, ctx: GoalActionContext) -> None:
        if ctx.owner_store is None or self.expect.expected_owner_pid is None:
            raise ValueError("Retry requires the executing owner's private goal authority.")

    def change(self, ctx: GoalActionContext) -> Goal:
        goal = ctx.require_goal()
        if not isinstance(goal.state, BlockedGoal):
            raise ValueError("The blocked goal changed; refresh its state.")
        store = ctx.owner_store
        assert store is not None
        generation = store.snapshot(goal.id)
        if generation is None:
            # Explicit owner decision can adopt a legacy registry-only goal.
            store.create_goal(goal.id)
            generation = store.snapshot(goal.id)
            assert generation is not None
        generation.lifecycle.authorize_retry(store, generation, uuid4().hex)
        return replace(goal, state=ActiveGoal(), revision=goal.revision + 1)
