"""Commander display derived from the native record owners in one read."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.commander_frame_file import CommanderFrameFile
from agent_comms.commander_pings import LoadPing, PhaseRowPing, Ping, UnitRowPing
from agent_comms.field_codec import projected
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.locked_store import JsonDocument
from agent_comms.plan_phases import PlanPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import OrderProgress, ProgressRead, PullRequest, UnmergedListedPullRequest
from agent_comms.tool_result import ToolResult
from agent_comms.turn_digests import OrderEditCountRead
from agent_comms.ui_model.unit_state import UnitClock, UnitState

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class CommanderOrder:
    """Small order card; identifiers/text come from Todo, review rounds from TodoStore."""

    problem: str
    text: str
    order: str
    state: str
    progress: OrderProgress
    review_timers_up: tuple[RecordedEventTime, ...]
    strikes: int
    unjudged_edits: int


@dataclass(frozen=True)
class CommanderUnit:
    """One unit's state card: its orders, live strikes and GitHub's open ready/draft requests.

    ``state`` and ``clock`` come from the UnitState the heartbeat derived for the unit, so a
    view tells moving from circling without deriving either itself.
    """

    unit: ThreadIncarnation
    state: type[UnitState]
    clock: UnitClock
    orders: tuple[CommanderOrder, ...]
    strikes: int
    ready_prs: tuple[PullRequest, ...]
    draft_prs: tuple[PullRequest, ...]

    def line(self) -> str:
        orders = "; ".join(
            f"{order.problem}: {order.state}; {order.progress.line}" for order in self.orders
        )
        return (
            f"{self.state.__name__} {self.unit.name}: orders={orders} "
            f"review timers={sum(len(order.review_timers_up) for order in self.orders)} "
            f"strikes={self.strikes} edits={sum(order.unjudged_edits for order in self.orders)} "
            f"ready={','.join(str(pr.number) for pr in self.ready_prs)} "
            f"draft={','.join(str(pr.number) for pr in self.draft_prs)}"
        )

    def ping(self) -> UnitRowPing:
        return UnitRowPing(self.unit, text=self.line())


@dataclass(frozen=True)
class CommanderPhase:
    """PlanPhase supplies every answer; no HUD status taxonomy is maintained beside it."""

    phase: type[PlanPhase]
    done: bool
    ready: bool
    holders: tuple[ThreadIncarnation, ...]

    def line(self) -> str:
        return (
            f"{self.phase.plan.declared_name}/{self.phase.declared_name} "
            f"done={self.done} ready={self.ready} "
            f"holders={','.join(unit.name for unit in self.holders)}"
        )

    def ping(self) -> PhaseRowPing:
        return PhaseRowPing(self.phase, text=self.line())


@dataclass(frozen=True)
class CommanderHud(ToolResult):
    """One disposable view of native records, shared by comms_hud and the commander board.

    TodoStore owns orders, review timers and strikes; OrderEditCountRead owns edit counts;
    PullRequests owns ready/draft facts; PlanPhase owns phase state; OverloadDegradation
    owns the load formula and decision. Nothing here writes or retains a board.
    """

    commander: ThreadIncarnation
    units: tuple[CommanderUnit, ...]
    phases: tuple[CommanderPhase, ...]
    load: OverloadDegradation

    @classmethod
    def read(cls, comms: Comms, commander: str) -> CommanderHud:
        frame = HeartbeatFrame.read(comms, commander, ProgressRead())
        return cls.of(comms, commander, frame, UnitState.current(comms, commander, frame))

    @classmethod
    def of(cls, comms: Comms, commander: str, frame: HeartbeatFrame,
           states: tuple[UnitState, ...]) -> CommanderHud:
        """Cards follow ``states``, already in the UnitState family's declared order."""
        snapshot, now = comms.registry.snapshot(), time.time()
        owner = snapshot.require(commander).incarnation
        store = comms.goals.todos
        pulls, reading = frame.pulls, frame.plan
        history = reading.todos
        held = frame.todos
        progress = pulls.progress(held, comms.registry)
        timers = store.timers_up_in(held, now, progress)
        edits = {
            read.todo.id: read.read()
            for read in OrderEditCountRead.for_orders(comms.root, frame.live)
        }
        units = []
        for state in states:
            unit, orders = state.unit, state.source.todos
            requests = {
                (todo.repo, request.number): request
                for todo in orders
                for request in pulls.listed(todo.repo, UnmergedListedPullRequest, todo)
            }
            units.append(CommanderUnit(
                unit, type(state), state.clock(),
                tuple(CommanderOrder(
                    todo.id, todo.text, todo.order.checked().id, todo.order.line, progress[todo.id],
                    tuple(at for due, at in timers if due.id == todo.id),
                    todo.strike_count, edits[todo.id],
                ) for todo in orders),
                sum(todo.strikes_against(unit) for todo in history),
                tuple(ready for request in requests.values() for ready in request.as_ready()),
                tuple(draft for request in requests.values() for draft in request.as_draft()),
            ))
        phases = tuple(CommanderPhase(
            phase, phase.done(reading), phase.ready(reading),
            tuple(dict.fromkeys(todo.held().owner_identity for todo in phase.holding(reading))),
        ) for phase in PlanPhase.members_with(PlanPhase))
        return cls(owner, tuple(units), phases,
                   OverloadDegradation.from_orders(held, len(timers), sum(edits.values())))

    def pings(self) -> tuple[Ping, ...]:
        return (
            LoadPing(text=str(self.load)),
            *(row.ping() for row in self.phases),
            *(row.ping() for row in self.units),
        )

    @projected(view="wire")
    def frame(self) -> tuple[str, ...]:
        """One line per declared phase or holder, with their original order and PR identities."""
        return tuple(ping.text for ping in self.pings())




@dataclass(frozen=True)
class CommanderFrame(CommanderFrameFile, JsonDocument[CommanderHud]):
    """The newest CommanderHud of each beating commander, for views that only show it.

    One writer, the heartbeat, replaces every commander's file each beat from the same
    CommanderHud its pings derive from, and keeps no history. It never reads a frame back,
    so a shape left by an older build cannot stop a beat; the next beat overwrites it.
    """

    path: Path

    record_type = CommanderHud

    def empty(self) -> CommanderHud:
        raise FileNotFoundError(self.path)

    @classmethod
    def replace_all(cls, root: Path, huds: tuple[CommanderHud, ...]) -> None:
        """Write one frame per commander; a commander no longer beating loses its frame."""
        written = set()
        for hud in huds:
            frame = cls(cls.path_in(root, lookup=str(hud.commander.require_recorded().lookup)))
            frame.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            frame._publish_unlocked(hud)
            written.add(frame.path)
        for stale in cls.matching_in(root) - written:
            stale.unlink(missing_ok=True)

    @classmethod
    def read_all(cls, root: Path) -> tuple[CommanderHud, ...]:
        return tuple(sorted((cls(path).read() for path in cls.matching_in(root)),
                            key=lambda hud: hud.commander.name))
