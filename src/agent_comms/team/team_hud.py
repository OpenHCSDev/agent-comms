"""Team display derived from the native record owners in one read."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import Annotated, Self

from agent_comms.cli_bridges.usage_reading import RateLimitReading
from agent_comms.comms import Comms
from agent_comms.coordination.service_passes import (
    BoardUnavailable,
    FailingPass,
    ServicePassStore,
    ServiceReading,
)
from agent_comms.installer.build_identity import InstalledBuild
from agent_comms.nominal.field_codec import (
    DisclosedRecord,
    HudExclude,
    PublicationExclude,
    WireExclude,
    WireTransient,
    projected,
)
from agent_comms.planning.plan_phase_base import Plan, PlanPhase
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.storage.locked_store import JsonDocument
from agent_comms.task_attachments.disclosure_text import ViewFacts, ViewSectionFacts, ViewTable
from agent_comms.task_attachments.team_pings import (
    AlarmGroup,
    AlarmSection,
    Ping,
    TeamDelivery,
    TeamUpdate,
)
from agent_comms.team.standing_rules import HeartbeatFrame
from agent_comms.team.team_frame_file import TeamFrameFile, TeamPlanFile
from agent_comms.team.team_hud_records import (
    FinishedPhases,
    HudSection,
    ProblemCard,
    Teammate,
    TeamOrder,
    TeamPhase,
)
from agent_comms.team.team_service import TeamService
from agent_comms.team.vibe_check import OverloadDegradation
from agent_comms.ui_model.teammate_state import TeammateState
from agent_comms_hooks.board import BoardEntry


@dataclass(frozen=True)
class TeamPlan:
    """The beat's plan phases: one copy for the whole team, which every frame reads."""

    phases: tuple[TeamPhase, ...] = ()

    def ready(self) -> tuple[type[PlanPhase], ...]:
        return tuple(phase.phase for phase in self.phases if phase.ready)


@dataclass(frozen=True)
class TeamPlanDocument(TeamPlanFile, JsonDocument[TeamPlan]):
    """The heartbeat replaces it with the frames, from the same board; it keeps no history."""

    path: Path
    reading: ServiceReading = field(default_factory=ServiceReading, compare=False, kw_only=True)

    record_type = TeamPlan

    @classmethod
    def at(cls, root: Path, reading: ServiceReading = ServiceReading()) -> Self:
        """A frozen empty reading is the default; readers with health pass theirs."""
        return cls(cls.path_in(root), reading=reading)

    def empty(self) -> TeamPlan:
        """No beat has published yet, so no phase is known ready: that is the empty plan.

        The plan is a *publication* of readiness the heartbeat computes by counting main. Its
        absence says only that nobody has counted, which is the same answer as "nothing is
        ready" for every reader of `ready()` — and an order that serves no phase is legal
        (`serves` is optional). A board *display* still refuses, through `TeamFrame.empty()`,
        where a missing teammate frame has no meaningful empty value; a phase lookup does not.
        Refusing here made every `agent-comms-team order` verb fail on a bus with no heartbeat,
        which is exactly the bus where someone needs to be sent an order to fix the heartbeat.
        """
        return TeamPlan()


@dataclass(frozen=True)
class UnreadableUsage(RateLimitReading):
    reason: str

    def line(self) -> str:
        return f'usage unreadable: {self.reason}'

    def needs_hand(self) -> bool:
        return True


@dataclass(frozen=True)
class CodexUsage:
    reading: RateLimitReading

    def line(self) -> str:
        return f"Codex {self.reading.line()}"

    def needs_hand(self) -> bool:
        return self.reading.needs_hand()


@dataclass(frozen=True)
class TeamStatus(DisclosedRecord):
    load: float
    reviews_due: int
    """Its label is derived from this name, and "Review Up: 0" told a reader nothing."""
    unjudged: int
    forks: int
    usage: str
    heartbeat: Annotated[str, PublicationExclude()]


@dataclass(frozen=True)
class HudDisplayFacts:
    teammate: ThreadIncarnation
    status: ViewFacts
    groups: tuple[ViewSectionFacts, ...]
    alarms: tuple[ViewSectionFacts, ...]
    alarm_membership: tuple[AlarmSection, ...]
    not_applied: tuple[str, ...]
    failures: tuple[tuple[str, FailingPass], ...]


@dataclass(frozen=True)
class TeamLoad(ServiceReading):
    """Retained load with the same recorded service evidence as its published frame."""

    load: OverloadDegradation

    def retained_status(self) -> str:
        """Whether what follows is being kept up to date, in the words a person uses.

        It shouted "STALE retained board rows and load:" while service_status separately
        announced "no failure recorded, so no heartbeat is running" - one fact, two
        vocabularies, stacked, which is most of why this header read as a block of
        unrelated text (Tristan, 2026-11-08, on the teams window: "there is like a big
        block of random text").
        """
        if self.services.heartbeat_current(self.heartbeat):
            return 'The team, right now:'
        return 'Nothing is refreshing these rows. Last time they were read:'

    def health_lines(self) -> tuple[str, ...]:
        return (*self.service_status, self.retained_status())

    @projected(view=WireExclude)
    def header(self) -> tuple[str, ...]:
        return (*self.health_lines(), str(self.load))


@dataclass(frozen=True)
class TeamHud(TeamLoad):
    """One disposable view of native records, shared by comms_hud and the team board.

    TodoStore owns orders, review timers and strikes; OrderEditCountRead owns edit counts;
    PullRequests owns ready/draft facts; PlanPhase owns phase state; OverloadDegradation
    owns the load formula and decision; ForkThread owns which threads are forks. Nothing here
    makes independent board decisions; its lines come from the retained observation.
    """

    teammate: ThreadIncarnation
    units: tuple[Teammate, ...]
    forks: tuple[ThreadIncarnation, ...]
    usage: CodexUsage
    packets: tuple[TeamUpdate, ...] = ()
    open_pings: tuple[Ping, ...] = field(kw_only=True)
    stopped: frozenset[str] = field(default=frozenset(), kw_only=True)
    phases: Annotated[tuple[TeamPhase, ...], WireTransient()] = field(default=(), kw_only=True)
    """The team plan's phases, read from TeamPlanDocument with the frame, never stored in it."""

    def with_plan(self, plan: TeamPlan) -> Self:
        return replace(self, phases=plan.phases)

    @classmethod
    def read(cls, comms: Comms, teammate: str) -> TeamHud:
        return TeamFrame.current(comms, teammate)

    @classmethod
    def of(cls, comms: Comms, frame: HeartbeatFrame,
           states: tuple[TeammateState, ...], *, delivery: TeamDelivery,
           usage: CodexUsage) -> TeamHud:
        """The one team board, prepared with its first teammate's delivery; for_delivery gives
        every other teammate the same board. Cards follow ``states``, already in the
        TeammateState family's declared order."""
        snapshot = comms.registry.snapshot()
        owner = delivery.packet.teammate.require_recorded()
        pulls, reading = frame.pulls, frame.plan
        todo_history = reading.todos
        held = frame.todos
        progress = pulls.progress(held, comms.registry)
        timers = frame.review_timers
        installed = InstalledBuild.read()
        edits = frame.edit_counts(comms)
        units = []
        for state in states:
            unit, orders = state.unit, state.source.todos
            requests = {
                (todo.repo, request.number): request
                for todo in orders
                for request in pulls.open_for(todo)
            }
            units.append(Teammate(
                unit, type(state), state.clock(),
                tuple(TeamOrder(
                    todo.id, todo.text, todo.order.checked().id, todo.order.installed_line(installed),
                    progress[todo.id],
                    tuple(at for due, at in timers if due.id == todo.id),
                    todo.strike_count, edits[todo.id],
                ) for todo in orders),
                sum(todo.strikes_against(unit) for todo in todo_history),
                tuple(ready for request in requests.values() for ready in request.as_ready()),
                tuple(draft for request in requests.values() for draft in request.as_draft()),
            ))
        phases = tuple(TeamPhase(
            phase, phase.done(reading), phase.ready(reading),
            tuple(dict.fromkeys(todo.held().owner_identity for todo in phase.holding(reading))),
        ) for phase in PlanPhase.members_with(PlanPhase))
        forks = tuple(fork.incarnation
                      for fork in TeamService.forks(snapshot, TeamService.running(snapshot))
                      if snapshot.status(fork.name).is_receiving())
        passes = ServicePassStore.at(comms.root).read()
        return cls(teammate=owner, units=tuple(units), phases=phases,
                   load=OverloadDegradation.from_orders(held, len(timers), sum(edits.values())),
                   forks=forks, usage=usage, packets=(delivery.packet,),
                   open_pings=delivery.open_pings, services=passes,
                   stopped=delivery.packet.stopped(snapshot),
                   heartbeat=passes.heartbeat_completed_at(owner))

    def for_delivery(self, delivery: TeamDelivery) -> TeamHud:
        """Keep the prepared shared board; packet history and completion belong to its recipient."""
        owner = delivery.packet.teammate.require_recorded()
        return replace(self, teammate=owner, packets=(delivery.packet,),
                       open_pings=delivery.open_pings).with_services(self.services, owner)

    @property
    def board(self) -> tuple[BoardEntry, ...]:
        """The one action-filtered set captured from Ping.on_board for this observation."""
        return tuple(ping.board_entry() for ping in self.open_pings)

    @cached_property
    def alarms(self) -> tuple[AlarmSection, ...]:
        return AlarmGroup.sections(self.packets, self.open_pings, self.stopped)

    @cached_property
    def groups(self) -> tuple[HudSection, ...]:
        finished = tuple(phase.phase for phase in self.phases if phase.finished)
        live = frozenset(phase.phase.plan for phase in self.phases if not phase.finished)
        folded = tuple(FinishedPhases(plan, phases)
                       for plan in Plan.members_with(Plan) if plan in live
                       for phases in (tuple(phase for phase in finished if phase.plan is plan),)
                       if phases)
        return HudSection.partition((
            # Problems first, each with every unit holding it. They were already here,
            # inside the unit cards, with Teammate.work flattening every one of a unit's
            # orders into a single cell; a card per problem is the same facts where they
            # can be read. Grouped by the order itself, so one problem held by three units
            # is one row naming three holders rather than three rows that read as three
            # problems - and so the table's unique-identity rule is not broken by the
            # second holder of anything.
            *(ProblemCard(order, tuple(holders)) for order, holders in self.held_problems().items()),
            *self.units, *(phase for phase in self.phases if not phase.finished), *folded,
        ))

    def held_problems(self) -> dict[TeamOrder, list[ThreadIncarnation]]:
        """Every problem on this board, and who holds it."""
        held: dict[TeamOrder, list[ThreadIncarnation]] = {}
        for unit in self.units:
            for order in unit.orders:
                held.setdefault(order, []).append(unit.unit)
        return held

    def status_table(self, now: float) -> ViewTable[TeamStatus]:
        return ViewTable(TeamStatus, HudExclude, (TeamStatus(
            self.load.total, self.load.timers_up, self.load.unjudged,
            len(self.forks), self.usage.line(), self.heartbeat.age(now),
        ),))

    def status_strip(self, now: float) -> str:
        return "\n".join((*self.health_lines(), *self.status_table(now).labelled_lines()))

    @cached_property
    def display_facts(self) -> HudDisplayFacts:
        """Compare declared displayed cells, not excluded tool clocks or paint-time ages."""
        alarms = self.alarms
        return HudDisplayFacts(
            self.teammate, self.status_table(0).facts,
            tuple(ViewSectionFacts(section.heading(), tuple(table.facts for table in section.tables))
                  for section in self.groups),
            tuple(ViewSectionFacts(section.heading(), tuple(table.facts for table in section.tables(0)))
                  for section in alarms),
            alarms,  # Original ping equality retains hidden overflow membership, not paint-time age/text.
            tuple(line for packet in self.packets for line in packet.not_applied),
            tuple(sorted(self.services.failing.items())),
        )

    @staticmethod
    def alarm_text(sections: tuple[AlarmSection, ...], now: float, *,
                   colour: bool = False) -> tuple[str, ...]:
        return tuple(line for section in sections for line in (
            section.group.paint(section.heading(), colour),
            *section.text_lines(now),
        ))

    @staticmethod
    def alarm_detail(sections: tuple[AlarmSection, ...], now: float) -> tuple[str, ...]:
        """Every original line; the section title already owns its compact heading."""
        return tuple(ping.render(now) for section in sections for ping in section.pings)

    def hud_text(self, now: float, *, colour: bool = False) -> tuple[str, ...]:
        """The heartbeat has no viewport; native readers own their concrete geometry."""
        result = list(self.status_strip(now).splitlines())
        # What is owed, by how much and for how long, before any row: one line per section.
        result.extend(("", *(section.digest(now) for section in self.alarms),
                       "Cards: " + ", ".join(section.heading() for section in self.groups)))
        result.extend(("", *self.alarm_text(
            tuple(section for section in self.alarms if section.group.needs_disposal()), now, colour=colour),
                       *(line for packet in self.packets for line in packet.not_applied)))
        for section in self.groups:
            group = section.group
            result.extend(("", group.paint(section.heading(), colour)))
            result.extend(group.paint(line, colour) for line in group.table_lines(
                table.folded_lines() for table in section.tables))
        result.extend(("", *self.alarm_text(
            tuple(section for section in self.alarms if not section.group.needs_disposal()), now, colour=colour)))
        return tuple(result)

    @projected(view=WireExclude)
    def frame(self) -> tuple[str, ...]:
        return self.hud_text(time.time())

    def load_reading(self) -> TeamLoad:
        return TeamLoad(self.load, services=self.services, heartbeat=self.heartbeat)


@dataclass(frozen=True)
class TeamFrame(TeamFrameFile, JsonDocument[TeamHud]):
    """The newest TeamHud of each beating teammate, for views that only show it.

    One writer, the heartbeat, replaces every teammate's file each beat from the same
    TeamHud the heartbeat reads, and keeps no history. It is a current view, never
    proof that a packet was successfully published.
    """

    path: Path
    reading: ServiceReading = field(default_factory=ServiceReading, compare=False, kw_only=True)

    record_type = TeamHud

    @classmethod
    def at(cls, root: Path, recipient: ThreadIncarnation) -> Self:
        return cls(cls.path_in(root, lookup=str(recipient.require_recorded().lookup)))

    def empty(self) -> TeamHud:
        raise BoardUnavailable(self.reading, f'Tristan frame missing: {self.path}')

    @classmethod
    def current(cls, comms: Comms, recipient: str) -> TeamHud:
        """Read the heartbeat's publication, never repeat its forge and history work. A fork
        reads the board of the teammate whose turn it works inside."""
        thread = comms.registry.require(recipient)
        owner = comms.registry.require(
            thread.teammate()).incarnation.require_recorded()
        passes = ServicePassStore.at(comms.root).read()
        health = ServiceReading().with_services(passes, owner)
        hud = cls(cls.path_in(comms.root, lookup=str(owner.lookup)), reading=health).read()
        if hud.teammate != owner:
            raise BoardUnavailable(health, f'Tristan frame belongs to another incarnation of {owner.name}')
        return hud.with_services(passes, owner).with_plan(
            TeamPlanDocument.at(comms.root, health).read())

    @classmethod
    def replace_all(cls, root: Path, plan: TeamPlan, huds: tuple[TeamHud, ...]) -> None:
        """Write the pass's team plan once and one frame per teammate; a teammate no longer
        beating loses its frame. A pass with no board writes an empty plan, never leaves the
        last one standing."""
        document = TeamPlanDocument.at(root)
        document.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        document._publish_unlocked(plan)
        for stale in cls.matching_in(root) - cls.write_all(root, huds):
            stale.unlink(missing_ok=True)

    @classmethod
    def write_all(cls, root: Path, huds: tuple[TeamHud, ...]) -> frozenset[Path]:
        """Write one frame per teammate in huds, unlinking nothing, and name what was written."""
        written = set()
        for hud in huds:
            frame = cls.at(root, hud.teammate)
            frame.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            frame._publish_unlocked(hud)
            written.add(frame.path)
        return frozenset(written)

    @classmethod
    def read_all(cls, root: Path) -> tuple[TeamHud, ...]:
        passes = ServicePassStore.at(root).read()
        health = ServiceReading(services=passes)
        frames = tuple(cls(path, reading=health).read() for path in cls.matching_in(root))
        plan = TeamPlanDocument.at(root, health).read() if frames else TeamPlan()
        huds = tuple(sorted((hud.with_services(passes, hud.teammate.require_recorded()).with_plan(plan)
            for hud in frames),
            key=lambda hud: hud.teammate.name))
        if not huds and passes.failing:
            raise BoardUnavailable(health, 'no published teammate frame.')
        return huds
