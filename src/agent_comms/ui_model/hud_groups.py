"""Every section the team board can show, in the order it shows them.

One owner, deliberately. These were declared in two modules - the unit groups in
teammate_state and the record groups in team_hud_records - while HudSection.partition
orders sections by HudGroup.members_with, which is declaration order. So the board's
reading order was an accident of which module imported which, nowhere stated, and
impossible to change without knowing that. Collecting the family here makes the order
explicit at its declaration owner, with no numeric ranks and no second roster for a
frontend to keep in step (paper1 Thm 4.17 on name and precedence conflicts; the ordering
answer is codex-latency's, 2026-11-08).

Nothing here imports the board's records or state, so a client that needs the order does
not pull in what fills it.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent_comms.task_attachments.disclosure_text import (
    AccentTone,
    ErrorTone,
    ViewHeading,
    WarningTone,
)


class ExpandedHudGroup:
    """Expanded sections consume their tables; collapsed sections do not prepare them."""

    @classmethod
    def table_lines(cls, tables: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
        return tuple(line for table in tables for line in table)


class HudGroup(ViewHeading, affix="Group"):
    """The declared order and headings shared by heartbeat and native table views."""

    @classmethod
    def table_lines(cls, tables: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
        return ()

    @classmethod
    def needs_hand(cls) -> bool:
        return False


class ProblemsGroup(ExpandedHudGroup, HudGroup):
    """Every problem, first, because they are what the board is about.

    Tristan, 2026-11-08, on the team window: there is no way to view all of the problems.
    They were in the frame the whole time, inside whichever unit card held each one, and
    flattened into a single newline-joined cell by Teammate.work - on screen and
    unreadable (BOUND-4). First in this family means first on the board.
    """


class UrgentGroup(ExpandedHudGroup, HudGroup):
    tone = WarningTone

    @classmethod
    def needs_hand(cls) -> bool:
        return True


class InProgressGroup(ExpandedHudGroup, HudGroup):
    """Ordinary ink, deliberately. It was SuccessTone, so the busiest group on the board
    was painted green - and a tone is a row's BACKGROUND (declared_table.py:125), so a
    team of working units was a wall of solid green for the one urgent row to compete
    with. Green also claims something finished well, and work in progress has not.
    Colour is left to the two groups that want something from a person."""


class ReadyToTestGroup(ExpandedHudGroup, HudGroup):
    tone = AccentTone


class IdleGroup(HudGroup):
    pass


class ParkedGroup(HudGroup):
    """Cards of teammates that read nothing now: one count, below the working groups."""


class PlanPhasesGroup(HudGroup):
    tone = AccentTone


class PhasesDoneByPlanGroup(HudGroup):
    """Each live plan's finished-phase count, folded to its heading: finished work is not what
    the board asks anyone to read. A plan with no unfinished phase leaves the board."""


class HudUnavailableGroup(HudGroup):
    tone = ErrorTone
