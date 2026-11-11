"""The reads a display constructs: their fields, wire names and results. Serving them is in
agent_comms.transport.tools.orders, which only the server imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.acp_extension import GoalSnapshotReply, TranscriptSnapshotUpdate
from agent_comms.execution.transcript_sources import (
    AtFrontier,
    LatestPage,
    PageLimit,
    PagePosition,
)
from agent_comms.input_disposition import DeliveryOverview
from agent_comms.nominal.field_codec import OrderId
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.team.team_hud_records import TeammateDetail
from agent_comms.transcript.transcripts import (
    LivePageSource,
    PageSource,
)
from agent_comms.transport.request import ReadRequest, ThreadReadRequest, tool_field


@dataclass(frozen=True, kw_only=True)
class TranscriptPageRead(
    ThreadReadRequest[TranscriptSnapshotUpdate], declared_name="comms_transcript_page",
):
    """One bounded transcript page with its current draft and the source witness that read it.
    The cursors and historical source select the page; they never choose whose authority the
    read runs under."""

    label = "Transcript page"
    description = (
        "Read one bounded page of a thread's transcript with its current draft. "
        "Send a returned cursor as an earlier_than or later_than position for the "
        "next page; page_source selects live history or an archive."
    )
    position: Annotated[
        PagePosition, *tool_field("latest_page, or earlier_than/later_than a returned cursor")
    ] = LatestPage()
    limit: Annotated[PageLimit, *tool_field("at_frontier, or through a cursor already shown")] = (
        AtFrontier()
    )
    page_source: Annotated[
        PageSource, *tool_field("Live history or a recorded archive identity")
    ] = LivePageSource()


@dataclass(frozen=True, kw_only=True)
class TeammateRead(ReadRequest[TeammateDetail], declared_name="comms_team_unit"):
    """One unit's card on a teammate's published frame, expanded with its orders' bounded
    instructions, stories, commits and activity. The held incarnations and orders select the
    exact card and witness it; they are never whose authority the read runs under."""

    label = "Team teammate"
    description = (
        "Expand one teammate's card on a teammate's published frame: its orders' "
        "bounded instructions, stories and commits, and its recent activity. "
        "Refuses when either incarnation or the teammate's assignment changed."
    )
    frame_owner: Annotated[
        ThreadIncarnation, *tool_field("Teammate incarnation whose published frame shows the card")
    ]
    unit: Annotated[ThreadIncarnation, *tool_field("Teammate incarnation on that card")]
    orders: Annotated[tuple[OrderId, ...], *tool_field("The card's orders, exactly as published")]


@dataclass(frozen=True, kw_only=True)
class InputDeliveryRead(
    ThreadReadRequest[DeliveryOverview], declared_name="comms_input_delivery",
):
    """One delivery projection, refined by the actual owner's queue when observed."""

    label = "Input delivery"
    description = ("Read a teammate's inputs that failed or are UNKNOWN, and its earlier delivery "
                   "notices, each with its bus sequence and input id; none is ever replayed.")
    include_history: Annotated[
        bool, *tool_field("Include earlier delivery notices; current inputs are always read")
    ] = True


@dataclass(frozen=True, kw_only=True)
class OwnerGoalRead(
    ThreadReadRequest[GoalSnapshotReply], declared_name="comms_owner_goal",
):
    """A unit's goal snapshot and the turn it was read with, answered by its live native owner
    under the owner's own registration and session; the thread selector only finds the owner."""

    label = "Owner goal"
    description = (
        "Read a teammate's current goal snapshot and the turn state its live native "
        "owner read with it."
    )
