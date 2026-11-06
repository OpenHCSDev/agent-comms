"""The team HUD's wire records: what a frame and a teammate's detail carry. Serving and
assembling them (the Codex usage reading, the heartbeat's frame) live in team_hud, so a client
that decodes a record never imports the bridges that read it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Annotated, ClassVar

from pydantic import JsonValue

from agent_comms.disclosure_text import AccentTone, ErrorTone, ViewTable
from agent_comms.nominal.field_codec import (
    DisclosedRecord,
    DisclosureIdentity,
    HudExclude,
    OrderId,
    projected,
)
from agent_comms.order_story import OrderStory
from agent_comms.plan_phases import Plan, PlanPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import OrderProgress, ProgressPoint, PullRequest
from agent_comms.tool_result import ToolResult
from agent_comms.ui_model.teammate_state import HudGroup, TeammateClock, TeammateState, UrgentGroup


@dataclass(frozen=True)
class TeamOrder(DisclosedRecord):
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


class PhasesDoneByPlanGroup(HudGroup):
    """Each live plan's finished-phase count, folded to its heading: finished work is not what
    the board asks anyone to read. A plan with no unfinished phase leaves the board."""


class HudUnavailableGroup(HudGroup):
    tone = ErrorTone


class HudRecord(DisclosedRecord, ABC):
    """Native rows expose their declared group, not another hand-joined renderer."""

    @property
    @abstractmethod
    def hud_group(self) -> type[HudGroup]: ...

    def needs_hand(self) -> bool:
        return self.hud_group.needs_hand()


@dataclass(frozen=True)
class Teammate(HudRecord):
    """One unit's state card: its orders, live strikes and GitHub's open ready/draft requests.

    ``state`` and ``clock`` come from the TeammateState the heartbeat derived for the unit, so a
    view tells moving from circling without deriving either itself.
    """

    unit: Annotated[ThreadIncarnation, DisclosureIdentity()]
    state: type[TeammateState]
    clock: Annotated[TeammateClock, HudExclude()] = field(compare=False)
    orders: Annotated[tuple[TeamOrder, ...], HudExclude()]
    strikes: Annotated[int, HudExclude()]
    ready_prs: Annotated[tuple[PullRequest, ...], HudExclude()]
    draft_prs: Annotated[tuple[PullRequest, ...], HudExclude()]

    @property
    def hud_group(self) -> type[HudGroup]:
        return self.state.hud_group

    @projected(view=HudExclude)
    def work(self) -> str:
        return "\n".join(f"{order.problem} {order.state}" for order in self.orders)

    @projected(view=HudExclude)
    def change(self) -> str:
        return "\n".join(order.progress.line for order in self.orders)

    @projected(view=HudExclude)
    def next(self) -> str:
        return self.state.action

    @property
    def order_ids(self) -> tuple[OrderId, ...]:
        return tuple(dict.fromkeys(card.order for card in self.orders))



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
        return tuple(cls(group, tuple(ViewTable(declaration, HudExclude, tuple(rows))
                                      for declaration, rows in grouped[group].items()))
                     for group in HudGroup.members_with(HudGroup) if group in grouped)


@dataclass(frozen=True)
class TeamOrderDetail(DisclosedRecord):
    """The selected card beside its original instructions, story and captured Git points."""

    card: Annotated[TeamOrder, DisclosureIdentity()]
    instructions: tuple[str, ...]
    story: OrderStory
    commits: tuple[ProgressPoint, ...]


@dataclass(frozen=True)
class TeammateDetail(ToolResult, DisclosedRecord):
    """A lazy disclosure; activity uses the existing bounded turn wire projection."""

    unit: Annotated[Teammate, DisclosureIdentity()]
    orders: tuple[TeamOrderDetail, ...]
    activity: JsonValue


@dataclass(frozen=True)
class TeamPhase(HudRecord):
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
    hud_group: ClassVar[type[HudGroup]] = PhasesDoneByPlanGroup

    @projected(view=HudExclude)
    def done(self) -> int:
        return len(self.phases)
