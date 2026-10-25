"""Commander display derived from the native record owners in one read."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import ClientError

from agent_comms.build_identity import InstalledBuild
from agent_comms.cli_bridges.codex import CodexAppServer
from agent_comms.cli_bridges.codex_protocol import RateLimitReading
from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.commander_frame_file import CommanderFrameFile
from agent_comms.commander_service import CommanderService
from agent_comms.field_codec import OrderId, projected
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.locked_store import JsonDocument
from agent_comms.plan_phases import Plan, PlanPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import OrderProgress, ProgressRead, PullRequest
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
    order: OrderId
    state: str
    progress: OrderProgress
    review_timers_up: tuple[RecordedEventTime, ...]
    strikes: int
    unjudged_edits: int


class HudRow(ABC):
    """One frame row below the load line. Each row says whether it needs the commander's hand,
    and the frame puts every row that does above every row that does not."""

    def needs_hand(self) -> bool:
        return False

    @abstractmethod
    def line(self) -> str: ...


@dataclass(frozen=True)
class CommanderUnit(HudRow):
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

    def needs_hand(self) -> bool:
        """Its UnitState asks for the commander, or a review timer on one of its orders is up."""
        return self.state.attention() or any(order.review_timers_up for order in self.orders)


@dataclass(frozen=True)
class CommanderPhase(HudRow):
    """PlanPhase supplies every answer; no HUD status taxonomy is maintained beside it."""

    phase: type[PlanPhase]
    done: bool
    ready: bool
    holders: tuple[ThreadIncarnation, ...]

    @property
    def finished(self) -> bool:
        """Done and released: nothing left for anyone, so it folds into its plan's count."""
        return self.done and not self.holders

    def needs_hand(self) -> bool:
        """Ready with nobody holding it, or done while an order still holds it open."""
        return (self.ready and not self.holders) or (self.done and bool(self.holders))

    def line(self) -> str:
        return (
            f"{self.phase.plan.declared_name}/{self.phase.declared_name} "
            f"done={self.done} ready={self.ready} "
            f"holders={','.join(unit.name for unit in self.holders)}"
        )


@dataclass(frozen=True)
class FinishedPhases(HudRow):
    """One plan's finished phases as one count: a graveyard needs no roll call."""

    plan: type[Plan]
    phases: tuple[type[PlanPhase], ...]

    def line(self) -> str:
        return f"{self.plan.declared_name}: {len(self.phases)} phases done"


class UnreadableUsage(RateLimitReading):
    reason: str

    def line(self) -> str:
        return f'usage unreadable: {self.reason}'

    def needs_hand(self) -> bool:
        return True


@dataclass(frozen=True)
class CodexUsage(HudRow):
    reading: RateLimitReading

    @classmethod
    def read(cls) -> CodexUsage:
        try:
            return cls(asyncio.run(CodexAppServer.rate_limits()))
        except (OSError, ClientError) as error:
            return cls(UnreadableUsage(reason=f'{type(error).__name__}: {error}'))

    def line(self) -> str:
        return self.reading.line()

    def needs_hand(self) -> bool:
        return self.reading.needs_hand()


@dataclass(frozen=True)
class CommanderForks(HudRow):
    """The commander's forks Core registered and still hears."""

    forks: tuple[ThreadIncarnation, ...]

    def line(self) -> str:
        return f"forks: {', '.join(fork.name for fork in self.forks) or 'none registered'}"


@dataclass(frozen=True)
class CommanderHud(ToolResult):
    """One disposable view of native records, shared by comms_hud and the commander board.

    TodoStore owns orders, review timers and strikes; OrderEditCountRead owns edit counts;
    PullRequests owns ready/draft facts; PlanPhase owns phase state; OverloadDegradation
    owns the load formula and decision; ThreadExecution owns which threads are forks. Nothing
    here writes or retains a board.
    """

    commander: ThreadIncarnation
    units: tuple[CommanderUnit, ...]
    phases: tuple[CommanderPhase, ...]
    load: OverloadDegradation
    forks: tuple[ThreadIncarnation, ...]
    usage: CodexUsage

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
        installed = InstalledBuild.read()
        edits = dict.fromkeys((todo.id for todo in held), 0) | {
            read.todo.id: read.read()
            for read in OrderEditCountRead.judgeable(comms.root, frame.live, progress)
        }
        units = []
        for state in states:
            unit, orders = state.unit, state.source.todos
            requests = {
                (todo.repo, request.number): request
                for todo in orders
                for request in pulls.open_for(todo)
            }
            units.append(CommanderUnit(
                unit, type(state), state.clock(),
                tuple(CommanderOrder(
                    todo.id, todo.text, todo.order.checked().id, todo.order.installed_line(installed),
                    progress[todo.id],
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
        forks = tuple(fork.incarnation for fork in CommanderService.forks(snapshot, commander)
                      if snapshot.status(fork.name).receiving)
        return cls(owner, tuple(units), phases,
                   OverloadDegradation.from_orders(held, len(timers), sum(edits.values())),
                   forks, CodexUsage.read())

    def rows(self) -> tuple[HudRow, ...]:
        """The rows under the load line: every row needing the commander's hand, in the
        UnitState family's order and then the plans' order, then the work that is moving, the
        forks, and one count per plan for its finished phases."""
        live = (*self.units, *(phase for phase in self.phases if not phase.finished))
        finished = tuple(phase.phase for phase in self.phases if phase.finished)
        return (
            *(row for row in live if row.needs_hand()),
            *(row for row in live if not row.needs_hand()),
            self.usage,
            CommanderForks(self.forks),
            *(FinishedPhases(plan, phases) for plan in Plan.members_with(Plan)
              for phases in (tuple(phase for phase in finished if phase.plan is plan),)
              if phases),
        )

    @projected(view="wire")
    def frame(self) -> tuple[str, ...]:
        """The load line, then one line per row in the order rows() declares."""
        return (str(self.load), *(row.line() for row in self.rows()))




@dataclass(frozen=True)
class CommanderFrame(CommanderFrameFile, JsonDocument[CommanderHud]):
    """The newest CommanderHud of each beating commander, for views that only show it.

    One writer, the heartbeat, replaces every commander's file each beat from the same
    CommanderHud the heartbeat reads, and keeps no history. It never reads a frame back,
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
