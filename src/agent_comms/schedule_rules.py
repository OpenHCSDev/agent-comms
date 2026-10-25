"""Independent owner-local scheduling barriers, using the shared rule family."""

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING



from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .drain_failures import drain_failures
from .activity import FailedWakeDiagnostic
from .errors import RelationViolationError
from .maintenance_barrier import MaintenanceStatus
from .reservation_rules import ReservationRule, RuleCheck, ReservationViolationError
from .routing import ScheduledTurn
from .turn_outcome import TurnOutcome
from .turn_input_source import NoTurnAcceptance

if TYPE_CHECKING:
    from .goals import GoalDeclaration
    from .coordinated_runtime import SelectedExecution
    from .input_drain import InputDrain
    from .thread_identity import OwnerIdentity
    from .turn_runner import TurnRunner


class WakeOutcome(DeclaredFamily, TurnOutcome):
    """A completed wake owns whether observation should run again."""

    @abstractmethod
    def settle(self, observer: Callable[[str], None], session_id: str) -> None: ...


@dataclass(frozen=True)
class WakeHeld(WakeOutcome):
    hold: MaintenanceStatus

    def settle(self, observer: Callable[[str], None], session_id: str) -> None:
        """Maintenance reopening supplies the next wake."""


class WakeDue(WakeOutcome):
    def settle(self, observer: Callable[[str], None], session_id: str) -> None:
        observer(session_id)


class SchedulesWake(RuleCheck):
    """A check that schedules a wake, so it waits for the previous wake to finish."""


class BackgroundWake(RuleCheck):
    """A check that starts work in the background, so configuration can disable it."""


@dataclass(frozen=True, kw_only=True)
class SessionScheduleCheck(RuleCheck):
    session_id: str
    inputs: "InputDrain"


@dataclass(frozen=True, kw_only=True)
class OwnerTurnScheduleCheck(SessionScheduleCheck):
    turns: "TurnRunner"


class ProjectResumeCheck(BackgroundWake, OwnerTurnScheduleCheck):
    """Start the turn the registry records as due after a project change.

    The registry entry is the only record, so an owner started later reads
    the same state and starts that turn too.
    """

    async def schedule(self) -> None:
        inputs, session_id = self.inputs, self.session_id
        try:
            self.require_valid()
            entry = (await inputs._own_entry(session_id)).entry
            self.require_valid()
        except ReservationViolationError:
            return
        if entry.status.running and entry.thread.owes_project_resume:
            resume = ScheduledTurn.resuming_in_project(str(entry.thread.worktree))
            pending = inputs.state(session_id).pending_turns
            if resume not in pending:
                pending.append(resume)
            WakeScheduleCheck(session_id=session_id, inputs=inputs).schedule()


class WakeScheduleCheck(SchedulesWake, BackgroundWake, SessionScheduleCheck):
    """Check wake permission and execute it under the existing turn lock.

    A queued wake never carries a stale owner grant across a lock wait. The
    current owner and active goal are read only after acquiring the turn lock.
    """

    def schedule(self) -> None:
        try:
            self.require_valid()
        except ReservationViolationError:
            return
        self.inputs.register_wake(
            self.session_id, self.inputs.runtime.background(self.run())
        )

    async def run(self) -> WakeOutcome:
        inputs, session_id = self.inputs, self.session_id
        while inputs.state(session_id).pending_turns:
            if ClosingScheduleRule().violated(self):
                return WakeDue()
            async with inputs.state(session_id).turn_lock:
                # The bus may accept a channel post while an operator drains the
                # fleet. Keep its existing queued origin; do not start a native
                # turn or consume it until this root's typed phase is READY.
                hold = await Coordination.run_worker(inputs.comms.owners.maintenance.read)
                if hold.turn_pending:
                    return WakeHeld(hold)
                snapshot = await Coordination.run_worker(inputs.comms.registry.snapshot)
                owner = snapshot.require(inputs.sessions.require(session_id))
                try:
                    snapshot.status(owner.name).require_running()
                except RelationViolationError:
                    inputs.state(session_id).pending_turns = []
                    continue
                state = inputs.state(session_id)
                pending, state.pending_turns = state.pending_turns, []
                goal = owner.goal.active()
                self._retain_goal_turns(pending, goal)
                if not pending:
                    continue
                pending, remaining = ScheduledTurn.take_batch(pending)
                if remaining:
                    state.pending_turns = remaining
                async with inputs.state(session_id).own_turn_task():
                    await inputs.effects.turns.run_agent_turn(
                        session_id,
                        inputs.sessions.require(session_id),
                        "\n\n".join(turn.prompt for turn in pending),
                        acceptance=NoTurnAcceptance(),
                        origins=tuple(origin for turn in pending for origin in turn.origin.messages()),
                        autonomous_goal=len(pending) == 1 and pending[0].autonomous_goal,
                    )
        return WakeDue()

    @staticmethod
    def _retain_goal_turns(pending: list[ScheduledTurn], goal: "GoalDeclaration") -> None:
        """Discard stale goal work without dropping independent incoming turns."""
        active_ids = frozenset(current.id for current in goal)
        pending[:] = [
            turn for turn in pending
            if not turn.autonomous_goal or turn.goal_id.retained_by(active_ids)
        ]


@dataclass(frozen=True, kw_only=True)
class SelectedWakeCheck(SchedulesWake, SessionScheduleCheck):
    """A selected turn runs beside the owner's observation, which hands it later messages."""

    execution: "SelectedExecution"
    thread: str
    owner: "OwnerIdentity"

    def schedule(self) -> None:
        try:
            self.require_valid()
        except ReservationViolationError:
            return  # The wake already running drains again when it ends.
        self.inputs.register_wake(
            self.session_id, self.inputs.runtime.background(self.run())
        )

    async def run(self) -> WakeOutcome:
        inputs, session_id = self.inputs, self.session_id
        effects = inputs.effects
        root_id = inputs.comms.owners.require_private_launch().wire_root_id
        # Selection may have been scheduled just before maintenance began.
        # A deferred selection is not a successful drain or diagnostic recovery.
        hold = await Coordination.run_worker(inputs.comms.owners.maintenance.read)
        if hold.turn_pending:
            return WakeHeld(hold)
        async with drain_failures(inputs.comms, self.thread, self.owner, FailedWakeDiagnostic):
            before = await inputs._observe_private_revision(session_id, root_id)
            result = await effects.turns.run_selected(session_id, self.execution)
            if result is not None:
                # A disconnected client must not turn a settled claim into an
                # apparent model failure. Reconnect reads the same durable row.
                await effects.cursors.publish(
                    session_id, self.thread, selected_status=result.cursor_status
                )
            else:
                # N (or absent-audience) rows prove coverage, not an injected
                # input. Extend only an existing current generation or an all-N prefix;
                # old-generation Pi evidence cannot initialize this cursor on reconnect.
                await effects.cursors.publish(session_id, self.thread, advance=True)
                if before == await inputs._observe_private_revision(session_id, root_id):
                    inputs.state(session_id).idle_revision = before
            if result is not None:
                # Messages this turn could not take are still pending.
                await inputs.drain_inbox(session_id)
        return WakeDue()


class ClosingScheduleRule(ReservationRule):
    check_type = SessionScheduleCheck
    explanation = "Input draining is closing."

    def violated(self, check):
        return check.inputs.closing


class OccupiedOwnerTurnRule(ReservationRule):
    check_type = OwnerTurnScheduleCheck
    explanation = "The session still owns a running turn."

    def violated(self, check):
        return check.turns.inputs.state(check.session_id).turn_task.busy


class NativeInboxOwnerTurnRule(ReservationRule):
    check_type = OwnerTurnScheduleCheck
    explanation = "A live native inbox still owns pending input."

    def violated(self, check):
        return check.inputs.state(check.session_id).turn_open()


class PendingWakeScheduleRule(ReservationRule):
    check_type = SchedulesWake
    explanation = "A previously scheduled wake has not finished."

    def violated(self, check):
        return check.inputs.state(check.session_id).wake_pending()


class PendingTurnOwnerTurnRule(ReservationRule):
    check_type = OwnerTurnScheduleCheck
    explanation = "Already queued turns have priority over a turn the owner starts itself."

    def violated(self, check):
        return bool(check.inputs.state(check.session_id).pending_turns)


class DisabledWakeScheduleRule(ReservationRule):
    check_type = BackgroundWake
    explanation = "Background waking is disabled by the input/runtime configuration."

    def violated(self, check):
        return check.inputs.background_wakes_disabled


class EmptyWakeScheduleRule(ReservationRule):
    check_type = WakeScheduleCheck
    explanation = "No queued turn requires a background wake."

    def violated(self, check):
        return not check.inputs.state(check.session_id).pending_turns
