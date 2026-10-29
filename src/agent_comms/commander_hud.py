"""Commander display derived from the native record owners in one read."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from aiohttp import ClientError
from pydantic import JsonValue

from agent_comms.build_identity import InstalledBuild
from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.cli_bridges.codex_protocol import RateLimitReading
from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.commander_frame_file import CommanderFrameFile
from agent_comms.commander_service import CommanderService
from agent_comms.errors import ToolRefusedError
from agent_comms.field_codec import DisclosureIdentity, OrderId, projected
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.locked_store import JsonDocument
from agent_comms.plan_phases import Plan, PlanPhase
from agent_comms.order_story import OrderStory
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.service_passes import ServicePassStore, ServiceReading
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import OrderProgress, ProgressPoint, ProgressRead, ProgressSource, PullRequest
from agent_comms.tool_result import ToolResult
from agent_comms.turn_digests import LastTurns, OrderEditCountRead
from agent_comms.ui_model.unit_state import UnitClock, UnitState
from agent_comms.task_attachments.commander_pings import CommanderUpdate
from agent_comms_hooks.board import BoardEntry

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class CommanderOrder:
    """Small order card; identifiers/text come from Todo, review rounds from TodoStore."""

    problem: Annotated[str, DisclosureIdentity()]
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

    unit: Annotated[ThreadIncarnation, DisclosureIdentity()]
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
            f"{self.unit.name}: {self.state.question} orders={orders} "
            f"review timers={sum(len(order.review_timers_up) for order in self.orders)} "
            f"strikes={self.strikes} edits={sum(order.unjudged_edits for order in self.orders)} "
            f"ready={','.join(str(pr.number) for pr in self.ready_prs)} "
            f"draft={','.join(str(pr.number) for pr in self.draft_prs)}"
        )

    def brief_parts(self) -> tuple[str, ...]:
        """The scan line's coherent pieces: the unit, each held order, its pull requests and
        next action. The view owns wrapping, including sections wider than its viewport."""
        orders = tuple(
            f"{order.problem} {order.state} · {order.progress.line}" for order in self.orders
        ) or ("no held orders",)
        pulls = ", ".join(f"#{pr.number}@{pr.head[:9]}" for pr in (*self.ready_prs, *self.draft_prs))
        return (self.unit.name, *orders, pulls or "no PR", f"→ {self.state.action}")

    def brief(self) -> str:
        """A dense scan line; expansion reads the original order and activity owners."""
        return " | ".join(self.brief_parts())

    def needs_hand(self) -> bool:
        """Its UnitState asks for the commander, or a review timer on one of its orders is up."""
        return (self.state.attention() and self.state.commander_disposes) or any(
            order.review_timers_up for order in self.orders
        )

    def detail(self, comms: Comms, commander: ThreadIncarnation,
               orders: tuple[OrderId, ...]) -> CommanderUnitDetail:
        """Expand this published selection from its original record owners."""
        if tuple(dict.fromkeys(card.order for card in self.orders)) != orders:
            raise ToolRefusedError("The selected unit's orders changed; read its current card")
        store = comms.goals.todos
        held = store.held_by(commander, self.unit)
        current = {todo.id: todo for todo in held}
        if {(todo.id, todo.order.checked().id) for todo in held} != {
            (card.problem, card.order) for card in self.orders
        }:
            raise ToolRefusedError("The selected unit's assignments changed; read its current card")
        pulls = ProgressRead(held)
        details = tuple(CommanderOrderDetail(
            card, (current[card.problem].order.checked().instructions(current[card.problem]),),
            OrderStory.collect(card.order, store.for_order(card.order)),
            tuple(ProgressSource.of(current[card.problem], comms.registry, pulls).commits()),
        ) for card in self.orders)
        turns = comms.transcripts.turns(self.unit.name, LastTurns())
        snapshot = comms.registry.snapshot()
        if turns.thread != self.unit or not self.unit.current(snapshot) or not commander.current(snapshot):
            raise ToolRefusedError("The selected incarnation changed while its details were read")
        return CommanderUnitDetail(self, details, turns.to_wire())


@dataclass(frozen=True)
class CommanderOrderDetail:
    """The selected card beside its original instructions, story and captured Git points."""

    card: Annotated[CommanderOrder, DisclosureIdentity()]
    instructions: tuple[str, ...]
    story: OrderStory
    commits: tuple[ProgressPoint, ...]


@dataclass(frozen=True)
class CommanderUnitDetail:
    """A lazy disclosure; activity uses the existing bounded turn wire projection."""

    unit: Annotated[CommanderUnit, DisclosureIdentity()]
    orders: tuple[CommanderOrderDetail, ...]
    activity: JsonValue


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
class BoardRow(HudRow):
    """A committed board line, without another interpretation or ordering."""

    entry: BoardEntry

    def needs_hand(self) -> bool:
        return True

    def line(self) -> str:
        return self.entry.aged(time.time())


@dataclass(frozen=True)
class QuietUnits(HudRow):
    """Units with no commander action share one roll call."""

    units: tuple[CommanderUnit, ...]

    def line(self) -> str:
        return f"{len(self.units)} units need no hand: " + ", ".join(
            unit.unit.name for unit in self.units
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
            return cls(asyncio.run(CodexConnection.rate_limits()))
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
class CommanderLoad(ServiceReading):
    """Retained load with the same recorded service evidence as its published frame."""

    load: OverloadDegradation

    @projected(view='wire')
    def frame(self) -> tuple[str, ...]:
        return (*self.service_status(), self.retained_status(), str(self.load))


@dataclass(frozen=True)
class CommanderHud(ServiceReading):
    """One disposable view of native records, shared by comms_hud and the commander board.

    TodoStore owns orders, review timers and strikes; OrderEditCountRead owns edit counts;
    PullRequests owns ready/draft facts; PlanPhase owns phase state; OverloadDegradation
    owns the load formula and decision; ThreadExecution owns which threads are forks. Nothing
    here makes independent board decisions; its lines come from the committed packet.
    """

    commander: ThreadIncarnation
    units: tuple[CommanderUnit, ...]
    phases: tuple[CommanderPhase, ...]
    load: OverloadDegradation
    forks: tuple[ThreadIncarnation, ...]
    usage: CodexUsage
    board: tuple[BoardEntry, ...] = ()

    @classmethod
    def read(cls, comms: Comms, commander: str) -> CommanderHud:
        return CommanderFrame.current(comms, commander)

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
            for read in OrderEditCountRead.judgeable(
                comms.root, frame.live, progress, frame.workplaces)
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
        board = tuple(entry for packet in CommanderUpdate.latest(comms, owner)
                      for entry in packet.board(comms, commander))
        return cls(owner, tuple(units), phases,
                   OverloadDegradation.from_orders(held, len(timers), sum(edits.values())),
                   forks, CodexUsage.read(), board)

    def rows(self) -> tuple[HudRow, ...]:
        """Committed board order, actionable cards, live phases, then folded quiet units."""
        live_phases = tuple(phase for phase in self.phases if not phase.finished)
        live = (*self.units, *live_phases)
        quiet = tuple(unit for unit in self.units if not unit.needs_hand())
        finished = tuple(phase.phase for phase in self.phases if phase.finished)
        return (
            *(BoardRow(line) for line in self.board),
            *(row for row in live if row.needs_hand()),
            *(phase for phase in live_phases if not phase.needs_hand()),
            *((QuietUnits(quiet),) if quiet else ()),
            self.usage,
            CommanderForks(self.forks),
            *(FinishedPhases(plan, phases) for plan in Plan.members_with(Plan)
              for phases in (tuple(phase for phase in finished if phase.plan is plan),)
              if phases),
        )

    def brief(self) -> tuple[str, ...]:
        """Routine heartbeat telemetry; the full board retains completed plans."""
        plans = "; ".join(
            f"{plan.declared_name} {sum(phase.finished for phase in phases)}/{len(phases)}"
            for plan in Plan.members_with(Plan)
            for phases in (tuple(phase for phase in self.phases if phase.phase.plan is plan),)
            if any(not phase.finished for phase in phases)
        )
        visible = tuple(unit for unit in self.units if unit.orders or unit.needs_hand())
        quiet = tuple(unit for unit in self.units if unit not in visible)
        return (
            f"{self.load} | {len(visible)} active units | {len(self.forks)} forks",
            self.usage.line(),
            *(unit.brief() for unit in visible),
            *((QuietUnits(quiet).line(),) if quiet else ()),
            *((f"Plans · {plans}",) if plans else ()),
        )

    @projected(view="wire")
    def frame(self) -> tuple[str, ...]:
        """The load line, then one line per row in the order rows() declares."""
        return (*self.service_status(), self.retained_status(), str(self.load),
                *(row.line() for row in self.rows()))

    def load_reading(self) -> CommanderLoad:
        return CommanderLoad(self.load, services=self.services, heartbeat=self.heartbeat)




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
        raise ToolRefusedError(f"Tristan frame missing: {self.path}")

    @classmethod
    def current(cls, comms: Comms, commander: str) -> CommanderHud:
        """Read the heartbeat's publication, never repeat its forge and history work."""
        owner = comms.registry.require(commander).incarnation.require_recorded()
        passes = ServicePassStore.at(comms.root).read()
        try:
            hud = cls(cls.path_in(comms.root, lookup=str(owner.lookup))).read()
        except ToolRefusedError as error:
            health = ServiceReading().with_services(passes, owner)
            raise ToolRefusedError('\n'.join((*health.service_status(),
                                             f'Board unavailable: {error}'))) from error
        if hud.commander != owner:
            raise ToolRefusedError(f"Tristan frame belongs to another incarnation of {owner.name}")
        return hud.with_services(passes, owner)

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
        passes = ServicePassStore.at(root).read()
        return tuple(sorted((hud.with_services(passes, hud.commander.require_recorded())
            for path in cls.matching_in(root) for hud in (cls(path).read(),)),
            key=lambda hud: hud.commander.name))
