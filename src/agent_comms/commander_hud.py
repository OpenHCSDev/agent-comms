"""Commander display derived from the native record owners in one read."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from functools import cached_property
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from aiohttp import ClientError
from pydantic import JsonValue

from agent_comms.build_identity import InstalledBuild
from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.cli_bridges.codex_protocol import RateLimitReading
from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.commander_frame_file import CommanderFrameFile
from agent_comms.commander_service import CommanderService
from agent_comms.coordinator import Coordination
from agent_comms.disclosure_text import (
    AccentTone, ErrorTone, ViewFacts, ViewSectionFacts, ViewTable,
)
from agent_comms.errors import ToolRefusedError
from agent_comms.field_codec import (
    DisclosureIdentity, HudExclude, OrderId, PublicationExclude, WireTransient, projected,
)
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.locked_store import JsonDocument
from agent_comms.plan_phases import Plan, PlanPhase
from agent_comms.order_story import OrderStory
from agent_comms.recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from agent_comms.service_passes import ServicePassStore
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import OrderProgress, ProgressPoint, ProgressRead, ProgressSource, PullRequest
from agent_comms.tool_result import ToolResult
from agent_comms.turn_digests import LastTurns, OrderEditCountRead
from agent_comms.ui_model.unit_state import ExpandedHudGroup, HudGroup, UnitClock, UnitState, UrgentGroup
from agent_comms.task_attachments.commander_pings import (
    AlarmGroup, AlarmSection, CommanderDelivery, CommanderUpdate, Ping,
)
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


class PlanPhasesGroup(HudGroup):
    tone = AccentTone


class FinishedPlansGroup(ExpandedHudGroup, HudGroup):
    pass


class HudUnavailableGroup(HudGroup):
    tone = ErrorTone


class HudRecord(ABC):
    """Native rows expose their declared group, not another hand-joined renderer."""

    @property
    @abstractmethod
    def hud_group(self) -> type[HudGroup]: ...

    def needs_hand(self) -> bool:
        return self.hud_group.needs_hand()


@dataclass(frozen=True)
class CommanderUnit(HudRecord):
    """One unit's state card: its orders, live strikes and GitHub's open ready/draft requests.

    ``state`` and ``clock`` come from the UnitState the heartbeat derived for the unit, so a
    view tells moving from circling without deriving either itself.
    """

    unit: Annotated[ThreadIncarnation, DisclosureIdentity()]
    state: type[UnitState]
    clock: Annotated[UnitClock, HudExclude()] = field(compare=False)
    orders: Annotated[tuple[CommanderOrder, ...], HudExclude()]
    strikes: Annotated[int, HudExclude()]
    ready_prs: Annotated[tuple[PullRequest, ...], HudExclude()]
    draft_prs: Annotated[tuple[PullRequest, ...], HudExclude()]

    @property
    def hud_group(self) -> type[HudGroup]:
        return self.state.hud_group

    @projected(view="hud")
    def work(self) -> str:
        return "\n".join(f"{order.problem} {order.state}" for order in self.orders)

    @projected(view="hud")
    def change(self) -> str:
        return "\n".join(order.progress.line for order in self.orders)

    @projected(view="hud")
    def next(self) -> str:
        return self.state.action

    @property
    def order_ids(self) -> tuple[OrderId, ...]:
        return tuple(dict.fromkeys(card.order for card in self.orders))

    def detail(self, comms: Comms, commander: ThreadIncarnation,
               orders: tuple[OrderId, ...]) -> CommanderUnitDetail:
        """Expand this published selection from its original record owners."""
        if self.order_ids != orders:
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
class HudSection:
    """One group heading, with native tables partitioned by their actual declarations."""

    group: type[HudGroup]
    tables: tuple[ViewTable[HudRecord], ...]

    @cached_property
    def records(self) -> tuple[HudRecord, ...]:
        return tuple(record for table in self.tables for record in table.records)

    def heading(self) -> str:
        return self.group.count_heading(len(self.records))

    @classmethod
    def partition(cls, records: Iterable[HudRecord]) -> tuple[HudSection, ...]:
        grouped: dict[type[HudGroup], dict[type[HudRecord], list[HudRecord]]] = {}
        for record in records:
            grouped.setdefault(record.hud_group, {}).setdefault(type(record), []).append(record)
        return tuple(cls(group, tuple(ViewTable(declaration, "hud", tuple(rows))
                                      for declaration, rows in grouped[group].items()))
                     for group in HudGroup.members_with(HudGroup) if group in grouped)


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
class CommanderPhase(HudRecord):
    """PlanPhase supplies every answer; no HUD status taxonomy is maintained beside it."""

    phase: Annotated[type[PlanPhase], DisclosureIdentity()]
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

    @property
    def hud_group(self) -> type[HudGroup]:
        return UrgentGroup if self.needs_hand() else PlanPhasesGroup


@dataclass(frozen=True)
class FinishedPhases(HudRecord):
    """One declaration-derived finished count per plan, through the same table projection."""

    plan: Annotated[type[Plan], DisclosureIdentity()]
    phases: Annotated[tuple[type[PlanPhase], ...], HudExclude()]
    hud_group: ClassVar[type[HudGroup]] = FinishedPlansGroup

    @projected(view="hud")
    def done(self) -> int:
        return len(self.phases)


class UnreadableUsage(RateLimitReading):
    reason: str

    def line(self) -> str:
        return f'usage unreadable: {self.reason}'

    def needs_hand(self) -> bool:
        return True


@dataclass(frozen=True)
class CodexUsage:
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
class CommanderStatus:
    load: float
    review_up: int
    unjudged: int
    forks: int
    usage: str
    heartbeat: Annotated[str, PublicationExclude()]


@dataclass(frozen=True)
class HudDisplayFacts:
    commander: ThreadIncarnation
    status: ViewFacts
    groups: tuple[ViewSectionFacts, ...]
    alarms: tuple[ViewSectionFacts, ...]
    alarm_membership: tuple[AlarmSection, ...]
    not_applied: tuple[str, ...]


@dataclass(frozen=True)
class CommanderHud(ToolResult):
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
    packets: tuple[CommanderUpdate, ...] = ()
    open_pings: tuple[Ping, ...] = field(kw_only=True)
    heartbeat: Annotated[EventTime, WireTransient()] = field(
        default_factory=UnrecordedEventTime, compare=False, kw_only=True,
    )
    """Read-time projection of ServicePassStore's clock; never written into the frame."""

    @classmethod
    def read(cls, comms: Comms, commander: str) -> CommanderHud:
        return CommanderFrame.current(comms, commander)

    @classmethod
    def of(cls, comms: Comms, commander: str, frame: HeartbeatFrame,
           states: tuple[UnitState, ...], *, delivery: CommanderDelivery,
           usage: CodexUsage) -> CommanderHud:
        """Cards follow ``states``, already in the UnitState family's declared order."""
        snapshot = comms.registry.snapshot()
        owner = snapshot.require(commander).incarnation.require_recorded()
        pulls, reading = frame.pulls, frame.plan
        todo_history = reading.todos
        held = frame.todos
        progress = pulls.progress(held, comms.registry)
        timers = frame.review_timers
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
                sum(todo.strikes_against(unit) for todo in todo_history),
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
                   forks, usage, (delivery.packet,), open_pings=delivery.open_pings,
                   heartbeat=ServicePassStore.at(comms.root).read().heartbeat_completed_at(owner))

    @property
    def board(self) -> tuple[BoardEntry, ...]:
        """The one action-filtered set captured from Ping.on_board for this publication."""
        return tuple(ping.board_entry() for ping in self.open_pings)

    @cached_property
    def alarms(self) -> tuple[AlarmSection, ...]:
        return AlarmGroup.sections(self.packets, self.open_pings)

    @cached_property
    def groups(self) -> tuple[HudSection, ...]:
        finished = tuple(phase.phase for phase in self.phases if phase.finished)
        folded = tuple(FinishedPhases(plan, phases) for plan in Plan.members_with(Plan)
                       for phases in (tuple(phase for phase in finished if phase.plan is plan),)
                       if phases)
        return HudSection.partition((*self.units, *(phase for phase in self.phases if not phase.finished),
                                     *folded))

    def status_table(self, now: float) -> ViewTable[CommanderStatus]:
        return ViewTable(CommanderStatus, "hud", (CommanderStatus(
            self.load.total, self.load.timers_up, self.load.unjudged,
            len(self.forks), self.usage.line(), self.heartbeat.age(now),
        ),))

    def status_strip(self, now: float) -> str:
        return "\n".join(self.status_table(now).labelled_lines())

    @cached_property
    def display_facts(self) -> HudDisplayFacts:
        """Compare declared displayed cells, not excluded tool clocks or paint-time ages."""
        alarms = self.alarms
        return HudDisplayFacts(
            self.commander, self.status_table(0).facts,
            tuple(ViewSectionFacts(section.heading(), tuple(table.facts for table in section.tables))
                  for section in self.groups),
            tuple(ViewSectionFacts(section.heading(), tuple(table.facts for table in section.tables(0)))
                  for section in alarms),
            alarms,  # Original ping equality retains hidden overflow membership, not paint-time age/text.
            tuple(line for packet in self.packets for line in packet.not_applied),
        )

    @staticmethod
    def alarm_text(sections: tuple[AlarmSection, ...], now: float, *,
                   colour: bool = False) -> tuple[str, ...]:
        return tuple(line for section in sections for line in (
            section.group.paint(section.heading(), colour),
            *(line for table in section.tables(now) for line in table.labelled_lines()),
        ))

    @staticmethod
    def alarm_detail(sections: tuple[AlarmSection, ...], now: float) -> tuple[str, ...]:
        """Every original line; the section title already owns its compact heading."""
        return tuple(ping.render(now) for section in sections for ping in section.pings)

    def hud_text(self, now: float, *, colour: bool = False) -> tuple[str, ...]:
        """The heartbeat has no viewport; native readers own their concrete geometry."""
        result = list(self.status_strip(now).splitlines())
        for section in self.groups:
            group = section.group
            result.extend(("", group.paint(section.heading(), colour)))
            result.extend(group.paint(line, colour) for line in group.table_lines(
                table.lines() for table in section.tables))
        result.extend(("", *self.alarm_text(self.alarms, now, colour=colour),
                       *(line for packet in self.packets for line in packet.not_applied)))
        return tuple(result)

    @projected(view="wire")
    def frame(self) -> tuple[str, ...]:
        return self.hud_text(time.time())




@dataclass(frozen=True)
class CommanderFrame(CommanderFrameFile, JsonDocument[CommanderHud]):
    """The newest CommanderHud of each beating commander, for views that only show it.

    One writer, the heartbeat, replaces every commander's file each beat from the same
    CommanderHud the heartbeat reads, and keeps no history. It is a current view, never
    proof that a packet was successfully published.
    """

    path: Path

    record_type = CommanderHud

    @classmethod
    def at(cls, root: Path, commander: ThreadIncarnation) -> Self:
        return cls(cls.path_in(root, lookup=str(commander.require_recorded().lookup)))

    def empty(self) -> CommanderHud:
        raise ToolRefusedError(f"Tristan frame missing: {self.path}")

    @classmethod
    def current(cls, comms: Comms, commander: str) -> CommanderHud:
        """Read the heartbeat's publication, never repeat its forge and history work."""
        owner = comms.registry.require(commander).incarnation.require_recorded()
        passes = ServicePassStore.at(comms.root).read()
        completed = passes.heartbeat_completed_at(owner)
        if not completed.since(time.time() - 3 * Coordination.watch_period):
            raise ToolRefusedError(
                f"Tristan stale for {owner.name} since {completed.clock()}: {passes.stall()}"
            )
        hud = cls.at(comms.root, owner).read()
        if hud.commander != owner:
            raise ToolRefusedError(f"Tristan frame belongs to another incarnation of {owner.name}")
        return replace(hud, heartbeat=completed)

    @classmethod
    def replace_all(cls, root: Path, huds: tuple[CommanderHud, ...]) -> None:
        """Write one frame per commander; a commander no longer beating loses its frame."""
        written = set()
        for hud in huds:
            frame = cls.at(root, hud.commander)
            frame.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            frame._publish_unlocked(hud)
            written.add(frame.path)
        for stale in cls.matching_in(root) - written:
            stale.unlink(missing_ok=True)

    @classmethod
    def read_all(cls, root: Path) -> tuple[CommanderHud, ...]:
        passes = ServicePassStore.at(root).read()
        return tuple(sorted((replace(hud, heartbeat=passes.heartbeat_completed_at(
            hud.commander.require_recorded()))
            for path in cls.matching_in(root) for hud in (cls(path).read(),)),
            key=lambda hud: hud.commander.name))
