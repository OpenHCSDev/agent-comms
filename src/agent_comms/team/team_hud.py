"""Team display derived from the native record owners in one read."""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import Annotated, Self

from pydantic import JsonValue

from agent_comms.cli_bridges.usage_reading import RateLimitReading
from agent_comms.comms import Comms
from agent_comms.coordination.service_passes import (
    BoardUnavailable,
    ServiceFailures,
    ServicePassStore,
    ServiceReading,
)
from agent_comms.installer.build_identity import InstalledBuild
from agent_comms.nominal.field_codec import (
    DisclosedRecord,
    FieldCodec,
    HudExclude,
    MalformedRecord,
    NullMember,
    PublicationExclude,
    RecordProblems,
    TaggedRecordMember,
    TaggedUnion,
    WireExclude,
    WireTransient,
    projected,
)
from agent_comms.nominal.shared_json import SharedJson
from agent_comms.planning.plan_phase_base import Plan, PlanPhase
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.storage.locked_store import JsonDocument
from agent_comms.task_attachments.disclosure_text import ViewFacts, ViewSectionFacts, ViewTable
from agent_comms.task_attachments.team_pings import (
    AlarmDisplay,
    AlarmGroup,
    AlarmSection,
    Ping,
    PublishedFacts,
    TeamDelivery,
    TeamUpdate,
)
from agent_comms.team.standing_rules import HeartbeatFrame
from agent_comms.team.team_frame_file import TeamBoardFile
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


class TeamBoardSelection(TaggedUnion, affix="TeamBoards"):
    """The recipients requested by visible board pages, shared by reader and view."""

    @classmethod
    def named(cls, name: str) -> TeamBoardSelection:
        """An empty user filter requests the whole board."""
        return NamedTeamBoards(frozenset((name,))) if name else AllTeamBoards()

    @abstractmethod
    def includes(self, recipient: ThreadIncarnation) -> bool: ...

    @abstractmethod
    def combined(self, other: TeamBoardSelection) -> TeamBoardSelection: ...

    @abstractmethod
    def with_names(self, names: frozenset[str]) -> TeamBoardSelection: ...


@dataclass(frozen=True)
class AllTeamBoards(NullMember, TeamBoardSelection):
    def includes(self, recipient: ThreadIncarnation) -> bool:
        return True

    def combined(self, other: TeamBoardSelection) -> TeamBoardSelection:
        return self

    def with_names(self, names: frozenset[str]) -> TeamBoardSelection:
        return self


@dataclass(frozen=True)
class NamedTeamBoards(TaggedRecordMember, TeamBoardSelection):
    names: frozenset[str]

    def includes(self, recipient: ThreadIncarnation) -> bool:
        return recipient.name in self.names

    def combined(self, other: TeamBoardSelection) -> TeamBoardSelection:
        return other.with_names(self.names)

    def with_names(self, names: frozenset[str]) -> TeamBoardSelection:
        return type(self)(self.names | names)


@dataclass(frozen=True)
class TeamPlan:
    """The beat's plan phases: one copy for the whole team, which every frame reads."""

    phases: tuple[TeamPhase, ...] = ()

    def ready(self) -> tuple[type[PlanPhase], ...]:
        return tuple(phase.phase for phase in self.phases if phase.ready)


@dataclass(frozen=True)
class TeamBoardCut:
    """One selected board value and the publication-owned identities it changed."""

    huds: tuple[TeamHud, ...] = ()
    changed: frozenset[ThreadIncarnation] = frozenset()


@dataclass(frozen=True)
class TeamBoardPublication:
    """One atomic plan, recipient-frame and changed-identity cut."""

    plan: TeamPlan = field(default_factory=TeamPlan)
    frames: SharedJson = field(default_factory=SharedJson)
    changed: tuple[ThreadIncarnation, ...] = ()


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
    heartbeat: Annotated[str, PublicationExclude()] = field(compare=False)


@dataclass(frozen=True)
class HudDisplayFacts:
    status: ViewFacts
    groups: tuple[ViewSectionFacts, ...]
    alarms: tuple[ViewSectionFacts, ...]
    alarm_membership: tuple[AlarmDisplay, ...]
    not_applied: tuple[str, ...]
    failures: ServiceFailures


@dataclass(frozen=True)
class HudDisplay:
    """The exact recipient-neutral sources of one published display."""

    status: TeamStatus
    groups: tuple[HudSection, ...]
    alarms: tuple[AlarmDisplay, ...]
    not_applied: tuple[str, ...]
    failures: ServiceFailures

    @cached_property
    def facts(self) -> HudDisplayFacts:
        return HudDisplayFacts(
            ViewTable(TeamStatus, HudExclude, (self.status,)).facts,
            tuple(ViewSectionFacts(section.heading(), tuple(table.facts for table in section.tables))
                  for section in self.groups),
            tuple(alarm.facts for alarm in self.alarms),
            self.alarms,
            self.not_applied,
            self.failures,
        )

    @cached_property
    def publication(self) -> PublishedFacts:
        return PublishedFacts.of(self.facts)


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
class TeamHudRecord(TeamLoad):
    """Captured board data; rendering belongs to the public TeamHud view."""

    teammate: ThreadIncarnation
    units: tuple[Teammate, ...]
    forks: tuple[ThreadIncarnation, ...]
    usage: CodexUsage
    packets: tuple[TeamUpdate, ...] = ()
    open_pings: tuple[Ping, ...] = field(kw_only=True)
    stopped: frozenset[str] = field(default=frozenset(), kw_only=True)
    phases: Annotated[tuple[TeamPhase, ...], WireTransient()] = field(default=(), kw_only=True)
    """The team plan's phases, read from the atomic board publication with the frame, never stored in it."""


@dataclass(frozen=True)
class TeamHud(TeamHudRecord):
    """One disposable view of native records, shared by comms_hud and the team board.

    TodoStore owns orders, review timers and strikes; OrderEditCountRead owns edit counts;
    PullRequests owns ready/draft facts; PlanPhase owns phase state; OverloadDegradation
    owns the load formula and decision; ForkThread owns which threads are forks. Nothing here
    makes independent board decisions; its lines come from the retained observation.
    """

    def with_plan(self, plan: TeamPlan) -> Self:
        return self if self.phases == plan.phases else replace(self, phases=plan.phases)

    @classmethod
    def of(cls, comms: Comms, frame: HeartbeatFrame,
           states: tuple[TeammateState, ...], *, delivery: TeamDelivery,
           usage: CodexUsage) -> TeamHud:
        """The one team board, prepared with its first teammate's delivery; for_delivery gives
        every other teammate the same board. Cards follow ``states``, already in the
        TeammateState family's declared order."""
        snapshot = frame.snapshot
        owner = delivery.packet.teammate.require_recorded()
        pulls, reading = frame.pulls, frame.plan
        todo_history = reading.todos
        held = frame.todos
        progress = pulls.progress(held, comms.registry, snapshot=snapshot)
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
        hud = replace(self, teammate=owner, packets=(delivery.packet,),
                      open_pings=delivery.open_pings).with_services(self.services, owner)
        # Units, problems and plan phases are the heartbeat's one shared board.  Dataclass
        # replacement deliberately drops cached properties; without carrying this derived
        # owner every recipient rebuilt every table and field-dispatch plan independently.
        object.__setattr__(hud, "groups", self.groups)
        object.__setattr__(hud, "plain_group_text", self.plain_group_text)
        displays = AlarmGroup.displays(hud.packets, hud.open_pings, hud.stopped)
        object.__setattr__(
            hud,
            "alarm_displays",
            self.alarm_displays if displays == self.alarm_displays else displays,
        )
        display = hud.display
        if display == self.display:
            object.__setattr__(hud, "display", self.display)
        return hud

    def retaining_rendering(self, previous: TeamHud | None) -> TeamHud:
        """Carry each derived owner only when its complete source remains unchanged.

        Service completion clocks remain fresh on ``self``. Recipient-neutral display
        sources retain their facts and folded words independently of dispatch binding.
        """
        if previous is None:
            return self
        if self == previous:
            for name in ("groups", "plain_group_text"):
                if name in previous.__dict__:
                    object.__setattr__(self, name, previous.__dict__[name])
        same_alarms = self.alarm_source == previous.alarm_source
        if same_alarms:
            for name in ("alarm_displays", "alarms"):
                if name in previous.__dict__:
                    object.__setattr__(self, name, previous.__dict__[name])
        if self.display == previous.display:
            object.__setattr__(self, "display", previous.display)
        return self

    @property
    def board(self) -> tuple[BoardEntry, ...]:
        """The one action-filtered set captured from Ping.on_board for this observation."""
        return tuple(ping.board_entry() for ping in self.open_pings)

    @cached_property
    def alarm_source(self) -> tuple[object, ...]:
        """The exact occurrences supplying alarm membership, words, actions and clocks."""
        return (
            self.stopped,
            tuple(
                (
                    type(packet),
                    packet.teammate,
                    packet.not_applied,
                    tuple(ping.occurrence_identity for ping in packet.handled),
                    tuple(ping.occurrence_identity for ping in packet.new_pings()),
                )
                for packet in self.packets
            ),
            tuple(ping.occurrence_identity for ping in self.open_pings),
        )

    @cached_property
    def publication_identity(self) -> tuple[object, ...]:
        """Every semantic value and exact occurrence supplying the stored recipient frame."""
        return self.display,

    @cached_property
    def alarm_displays(self) -> tuple[AlarmDisplay, ...]:
        return AlarmGroup.displays(self.packets, self.open_pings, self.stopped)

    @cached_property
    def alarms(self) -> tuple[AlarmSection, ...]:
        return tuple(map(AlarmSection, self.alarm_displays))

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

    def status(self, now: float) -> TeamStatus:
        return TeamStatus(
            self.load.total, self.load.timers_up, self.load.unjudged,
            len(self.forks), self.usage.line(), self.heartbeat.age(now),
        )

    def status_table(self, now: float) -> ViewTable[TeamStatus]:
        return ViewTable(TeamStatus, HudExclude, (self.status(now),))

    def status_strip(self, now: float) -> str:
        return "\n".join((*self.health_lines(), *self.status_table(now).labelled_lines()))

    def group_text(self, *, colour: bool = False) -> tuple[str, ...]:
        """The shared board cards, independent of packet recipient and paint-time ages."""
        result: list[str] = []
        for section in self.groups:
            group = section.group
            result.extend(("", group.paint(section.heading(), colour)))
            result.extend(group.paint(row, colour) for row in group.table_lines(
                table.folded_lines() for table in section.tables
            ))
        return tuple(result)

    @cached_property
    def plain_group_text(self) -> tuple[str, ...]:
        return self.group_text()

    @cached_property
    def display(self) -> HudDisplay:
        """The publication owner: content sources only; recipient routing stays on the HUD."""
        return HudDisplay(
            self.status(0),
            self.groups,
            self.alarm_displays,
            tuple(line for packet in self.packets for line in packet.not_applied),
            ServiceFailures.of(self.services),
        )

    @staticmethod
    def alarm_text(sections: tuple[AlarmSection, ...], now: float, *,
                   colour: bool = False) -> tuple[str, ...]:
        return tuple(line for section in sections for line in (
            section.display.group.paint(section.display.heading(), colour),
            *section.text_lines(now),
        ))

    @staticmethod
    def alarm_detail(sections: tuple[AlarmSection, ...], now: float) -> tuple[str, ...]:
        """Every original line; the section title already owns its compact heading."""
        return tuple(ping.render(now) for section in sections for ping in section.display.pings)

    def hud_text(self, now: float, *, colour: bool = False) -> tuple[str, ...]:
        """The heartbeat has no viewport; native readers own their concrete geometry."""
        result = list(self.status_strip(now).splitlines())
        # What is owed, by how much and for how long, before any row: one line per section.
        result.extend(("", *(section.display.digest(now) for section in self.alarms),
                       "Cards: " + ", ".join(section.heading() for section in self.groups)))
        result.extend(("", *self.alarm_text(
            tuple(section for section in self.alarms
                  if section.display.group.needs_disposal()), now, colour=colour),
                       *(line for packet in self.packets for line in packet.not_applied)))
        result.extend(self.group_text(colour=True) if colour else self.plain_group_text)
        result.extend(("", *self.alarm_text(
            tuple(section for section in self.alarms
                  if not section.display.group.needs_disposal()), now, colour=colour)))
        return tuple(result)

    @projected(view=WireExclude)
    def frame(self) -> tuple[str, ...]:
        return self.hud_text(time.time())

    def load_reading(self) -> TeamLoad:
        return TeamLoad(self.load, services=self.services, heartbeat=self.heartbeat)


@dataclass(frozen=True)
class RetainedTeamBoard:
    """The decoded board and the exact publication that supplied it."""

    publication: TeamBoardPublication
    huds: tuple[TeamHud, ...]

    def packet_map(self) -> dict[ThreadIncarnation, tuple[TeamUpdate, ...]]:
        return {hud.teammate: hud.packets for hud in self.huds}


@dataclass(frozen=True)
class TeamBoardPublicationStore(
    TeamBoardFile,
    JsonDocument[TeamBoardPublication],
):
    """The only retained board writer and reader."""

    path: Path
    record_type = TeamBoardPublication

    @classmethod
    def at(cls, root: Path) -> Self:
        return cls(cls.path_in(root))

    def empty(self) -> TeamBoardPublication:
        return TeamBoardPublication()

    @staticmethod
    def frame_records(frames: SharedJson) -> dict[ThreadIncarnation, JsonValue]:
        recipient = FieldCodec.field_plan(TeamHudRecord, "teammate")
        records = tuple(
            (recipient.read(raw), raw)
            for raw in frames.expanded()
        )
        identities = tuple(
            owner.require_recorded().lookup for owner, _raw in records
        )
        if len(identities) != len(set(identities)):
            raise MalformedRecord("A team snapshot repeats a recipient")
        return dict(records)

    def records(
        self,
        publication: TeamBoardPublication | None = None,
    ) -> dict[ThreadIncarnation, JsonValue]:
        publication = self.read() if publication is None else publication
        return self.frame_records(publication.frames)

    def huds(
        self,
        selection: TeamBoardSelection = AllTeamBoards(),
        publication: TeamBoardPublication | None = None,
    ) -> tuple[TeamHud, ...]:
        return FieldCodec.decode_shared(
            tuple[TeamHud, ...],
            [
                raw
                for recipient, raw in self.records(publication).items()
                if selection.includes(recipient)
            ],
        )

    def retained(
        self,
        publication: TeamBoardPublication | None = None,
    ) -> RetainedTeamBoard:
        """Decode one publication once and retain the source that makes reuse valid."""
        publication = self.read() if publication is None else publication
        return RetainedTeamBoard(
            publication,
            tuple(
                hud.with_plan(publication.plan)
                for hud in self.huds(publication=publication)
            ),
        )

    def packet_map(
        self,
        publication: TeamBoardPublication | None = None,
    ) -> dict[ThreadIncarnation, tuple[TeamUpdate, ...]]:
        """Per-recipient packets derived from the one shared retained frame cut."""
        records = self.records(publication)
        packets = FieldCodec.record_key(TeamHudRecord, "packets")
        decoded = FieldCodec.decode_shared(
            tuple[tuple[TeamUpdate, ...], ...],
            [raw[packets] for raw in records.values()],
        )
        return dict(zip(records, decoded, strict=True))

    @staticmethod
    def captured_frames(huds: tuple[TeamHud, ...]) -> SharedJson:
        recipients = {
            hud.teammate.require_recorded().lookup: hud for hud in huds
        }
        return SharedJson.of(
            tuple(recipients.values()),
            tuple[TeamHudRecord, ...],
        )

    @staticmethod
    def retained_huds(
        plan: TeamPlan,
        huds: tuple[TeamHud, ...],
    ) -> tuple[TeamHud, ...]:
        """The semantic values actually retained by the recipient-keyed frame cut."""
        recipients = {
            hud.teammate.require_recorded().lookup: hud.with_plan(plan)
            for hud in huds
        }
        return tuple(recipients.values())

    @classmethod
    def publication(
        cls,
        plan: TeamPlan,
        huds: tuple[TeamHud, ...],
        before: dict[ThreadIncarnation, TeamHud],
    ) -> TeamBoardPublication:
        retained = cls.retained_huds(plan, huds)
        frames = cls.captured_frames(retained)
        after = {hud.teammate: hud for hud in retained}
        changed = tuple(sorted(
            (owner for owner in before.keys() | after.keys()
             if owner not in before or owner not in after
             or before[owner].publication_identity != after[owner].publication_identity),
            key=lambda owner: (owner.name, owner.created_at),
        ))
        return TeamBoardPublication(plan, frames, changed)

    @classmethod
    def replace_all(
        cls,
        root: Path,
        plan: TeamPlan,
        huds: tuple[TeamHud, ...],
        retained: RetainedTeamBoard | None = None,
    ) -> RetainedTeamBoard:
        store = cls.at(root)
        store.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            previous = retained.publication if retained is not None else store.read()
        except RecordProblems:
            successor = cls.publication(plan, huds, {})
            store._publish_unlocked(successor)
            return RetainedTeamBoard(
                successor,
                cls.retained_huds(plan, huds),
            )
        if retained is None or retained.publication != previous:
            retained = store.retained(previous)
        before = {hud.teammate: hud for hud in retained.huds}
        after = {
            hud.teammate: hud
            for hud in cls.retained_huds(plan, huds)
        }
        same_frames = before.keys() == after.keys() and all(
            before[owner].publication_identity == hud.publication_identity
            for owner, hud in after.items()
        )
        if previous.plan == plan and same_frames:
            # SharedJson is a storage encoding, not the board's equality owner.  Re-encoding
            # equal HUDs may choose different reference positions; comparing those raw graphs
            # made every recipient look changed and woke every reader on every heartbeat.
            if previous.changed:
                previous = replace(previous, changed=())
                store._publish_unlocked(previous)
            return RetainedTeamBoard(previous, tuple(after.values()))
        successor = cls.publication(plan, huds, before)
        if successor != previous:
            store._publish_unlocked(successor)
        return RetainedTeamBoard(successor, tuple(after.values()))

    @classmethod
    def write_all(
        cls,
        root: Path,
        huds: tuple[TeamHud, ...],
        retained_board: RetainedTeamBoard | None = None,
    ) -> frozenset[Path]:
        if not huds:
            return frozenset()
        store = cls.at(root)
        publication = store.read()
        if retained_board is None or retained_board.publication != publication:
            retained_board = store.retained(publication)
        huds_before = retained_board.huds
        before = {
            hud.teammate: hud for hud in huds_before
        }
        retained_huds = {
            hud.teammate.require_recorded().lookup: hud for hud in huds_before
        }
        retained_huds.update(
            (hud.teammate.require_recorded().lookup, hud) for hud in huds
        )
        store._publish_unlocked(cls.publication(
            publication.plan, tuple(retained_huds.values()), before
        ))
        return frozenset((store.path,))

    @classmethod
    def read_cut(
        cls,
        root: Path,
        selection: TeamBoardSelection = AllTeamBoards(),
    ) -> TeamBoardCut:
        passes = ServicePassStore.at(root).read()
        health = ServiceReading(services=passes)
        store = cls.at(root)
        publication = store.read()
        frames = store.huds(selection, publication)
        huds = tuple(
            sorted(
                (
                    hud.with_services(
                        passes, hud.teammate.require_recorded()
                    ).with_plan(publication.plan)
                    for hud in frames
                ),
                key=lambda hud: hud.teammate.name,
            )
        )
        if not huds and passes.failing:
            raise BoardUnavailable(health, "no published teammate frame.")
        return TeamBoardCut(
            huds,
            frozenset(owner for owner in publication.changed if selection.includes(owner)),
        )

    @classmethod
    def read_all(
        cls,
        root: Path,
        selection: TeamBoardSelection = AllTeamBoards(),
    ) -> tuple[TeamHud, ...]:
        return cls.read_cut(root, selection).huds

    @classmethod
    def read_for(cls, comms: Comms, recipient: str) -> TeamHud:
        thread = comms.registry.require(recipient)
        owner = comms.registry.require(
            thread.teammate()
        ).incarnation.require_recorded()
        passes = ServicePassStore.at(comms.root).read()
        health = ServiceReading().with_services(passes, owner)
        store = cls.at(comms.root)
        publication = store.read()
        records = store.records(publication)
        if owner not in records:
            raise BoardUnavailable(
                health,
                f"Tristan frame missing for {owner.name}: {store.path}",
            )
        hud = FieldCodec.decode(TeamHud, records[owner])
        return hud.with_services(passes, owner).with_plan(publication.plan)
