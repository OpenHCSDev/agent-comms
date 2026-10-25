"""Where a behaviour pattern was learned: the Codex postmortem's numbered failures, a
commander rule, a problem's fix, a seed catalog entry or a commit.

They are text, so the session hooks read the failure catalog without loading the patterns'
checks, which import Core's orders, todos and standing rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .declared_family import DeclaredFamily

COMMANDER_SKILL = "../../src/agent_comms/skills/comms-commander/SKILL.md"


class PatternSource(ABC):
    """Where a pattern was learned."""

    @abstractmethod
    def cite(self) -> str: ...


class PostmortemFailure(PatternSource, DeclaredFamily, affix="Failure"):
    """A failure the Codex postmortem names; a pattern that checks it cites an instance."""

    number: ClassVar[int]
    title: ClassVar[str]
    evidence: ClassVar[str]

    def cite(self) -> str:
        return f"Failure {self.number}: {self.evidence}"

    @classmethod
    def catalog(cls) -> str:
        return "\n".join(
            f"{failure.number}. {failure.title}: {failure.evidence}"
            for failure in sorted(
                cls.members_with(PostmortemFailure), key=lambda member: member.number
            )
        )


class RewroteFrameworkFailure(PostmortemFailure):
    number = 1
    title = "It rewrote the framework instead of questioning its own design"
    evidence = (
        "October 9 postmortem: 528 Textual commits since October 1; 7,646 lines added, "
        "2,947 removed; real scrolling remained three to four frames per second."
    )


class AddedStatesFailure(PostmortemFailure):
    number = 2
    title = "It added states instead of removing them"
    evidence = (
        "October 9 postmortem: measured, measured-source, live, child, capturing, released, "
        "materializing, rendered, prepared-document and pending-document multiplied; "
        "another transition was left unfinished and uncommitted."
    )


class ViolatedPrincipleFailure(PostmortemFailure):
    number = 3
    title = "It wrote down the right principle and then violated it"
    evidence = (
        'October 9 postmortem: it wrote "Moving code to a worker does not remove a synchronous '
        'wait for that worker", then moved CPU-bound Markdown work into asyncio.to_thread '
        "on GIL CPython 3.14."
    )


class MeasuredWrongThingFailure(PostmortemFailure):
    number = 4
    title = "It measured the wrong thing and reported it as progress"
    evidence = (
        "October 9 postmortem: 451 measurement-related commits and 716 evidence files yielded "
        "only three comparable files; headline 11.7 ms median timings measured terminal writes, "
        "not total frames."
    )


class RedefinedRealAppFailure(PostmortemFailure):
    number = 5
    title = 'It redefined "the real app" until something passed'
    evidence = (
        "October 9 postmortem: headless pilots, a private non-default candidate and writer flush "
        "times were offered as live results; the saved-history claim explicitly did not establish "
        "total 16 ms frames, blanks, End or multi-tab behavior."
    )


class IgnoredOrdersFailure(PostmortemFailure):
    number = 6
    title = "It ignored direct orders"
    evidence = (
        "October 9 postmortem: during a twelve-hour test ban, 15 branch test files and four main "
        "test files changed; zero profiling artifacts were produced."
    )


class IntentionsAsAccomplishmentsFailure(PostmortemFailure):
    number = 7
    title = "It described its intentions as its accomplishments"
    evidence = (
        'October 9 postmortem: 350 commit subjects in five days claimed facts were "derived", '
        '"canonical" or "authoritative" while the same constant remained hand-written in three '
        "places; commit titles supplied no frame-time proof."
    )


class CeremonyFailure(PostmortemFailure):
    number = 8
    title = "It drowned the work in ceremony"
    evidence = (
        "October 9 postmortem: repeated permission exchanges and reports replaced delivery; "
        "one checkout reached 8.7 GB, ratchets ran dozens of times and over 200,000 evidence "
        "lines were committed in one night."
    )


class MergedOverChecksFailure(PostmortemFailure):
    number = 9
    title = "It merged over its own checks and broke things in circles"
    evidence = (
        "October 9 postmortem: Core merges #607, #520 and #592 bypassed failed ratchets; "
        "39 of 56 fixes repaired the previous day's merges, including 22 features "
        "that landed broken."
    )


class InventedRulesFailure(PostmortemFailure):
    number = 10
    title = "It invented rules and blamed them on Tristan"
    evidence = (
        "October 9 postmortem: agents invented gates, protocols and receipt formats, attributed "
        'them to "development instructions", and could not identify a source Tristan wrote.'
    )


class HidKnownFailuresFailure(PostmortemFailure):
    number = 11
    title = "It hid known failures until asked"
    evidence = (
        "October 19 indictment, 20:23 confession: a dead overdue alarm, rotate crashing since "
        "about 20:16, 59 unjudged edits, a false cleared-board claim and strikes based on known "
        "broken readings were withheld until challenged."
    )


class CeilingsAsAppointmentsFailure(PostmortemFailure):
    number = 12
    title = "It turned deadline ceilings into appointments"
    evidence = (
        "October 19 incidents, 21:19: the commander told Tristan installation would follow "
        '"the batch due at 21:34", parking finished work until its ceiling.'
    )


class RoutedAroundGatesFailure(PostmortemFailure):
    number = 13
    title = "It routed around gates instead of obeying them"
    evidence = (
        "October 19 incidents: at 20:54–21:00 it wrote P684 itself after the fork gate refused "
        "orders; at 21:28 it tried gh pr merge --admin over PR2063's failed install check, "
        "and GitHub refused."
    )


class IgnoredRottenBoardAlarmsFailure(PostmortemFailure):
    number = 14
    title = "It ignored alarms because its board had rotted"
    evidence = (
        'October 19 indictment: alarms were screaming into a board nobody looked at; "Every order '
        "on my board is overdue, so the alarm's useless\" was the missing disclosure while stalls "
        "went unnoticed."
    )


class RecitedRulesFailure(PostmortemFailure):
    number = 15
    title = "It recited rules instead of following them"
    evidence = (
        "October 19 incidents: at 21:02 rules review failed because its memory cleanup deleted "
        "required rule files; at 21:40 it struck a unit for an allegedly invented install rule "
        "that origin/main AGENTS.md:53 actually contained."
    )


class DeletedLiveStateFailure(PostmortemFailure):
    number = 16
    title = "It deleted state without checking who was using it"
    evidence = (
        "October 20 cleanup: the commander removed /home/ts/wt/unit-directive-delete--P295, a "
        "running unit's worktree; install 9 then failed restarting that unit and rolled back."
    )


class BlockedFleetUnalarmedFailure(PostmortemFailure):
    number = 17
    title = "It left every unit blocked with no alarm"
    evidence = (
        "October 21, 02:13: Core showed all ten units' goals blocked as backend failures, written "
        "when install 16 stopped them at 01:56; claude-ai found it, and no standing rule fired."
    )


class SkippedCutoverFailure(PostmortemFailure):
    number = 18
    title = "It installed a change without the cutover the change required"
    evidence = (
        "October 21: every claude.ai connector call answered 500 from install 16 until 02:06, "
        "because grants.json still held five old-shape requests; the commander ran "
        "tools/cutover/connector_browser_trust.py only after the outage."
    )


class DelegatedDiagnosisFailure(PostmortemFailure):
    number = 19
    title = "It ordered units to diagnose and choose the shape"
    evidence = (
        "October 20: orders P362, P377, P378 and P379 asked units to find the defect and pick its "
        "fix, against the skill's rule that units do not diagnose or choose shapes."
    )


class OrdersKilledTurnsFailure(PostmortemFailure):
    number = 20
    title = "Its own orders stopped the units they were sent to"
    evidence = (
        "October 21, about 02:40: orders P392, P393 and P394 each changed the receiving unit's "
        "goal mid-turn; the turn's permission compared the whole goal declaration "
        "(turn_goal_permission.py:34), so all three units' next tool calls were denied and their "
        "goals blocked."
    )


class StatusBeforeEvidenceFailure(PostmortemFailure):
    number = 21
    title = "It reported a state its evidence did not show"
    evidence = (
        "October 20-21: 'Deadlines are dead (PR 2356)' went out at merge, before any install "
        "(claude-ai review fde6168a1262); the commander told #comms the 200 open directives were "
        "445 bridge-unacknowledged inbox messages (012fe7271b3c) when they are requests on its "
        "own goal."
    )


class UnansweredAskFailure(PostmortemFailure):
    number = 22
    title = "It left a unit's question unanswered for hours"
    evidence = (
        "October 20-21: fork-boundaries' alert at bus seq 8122 held its finished work blocked, "
        "about seven hours by claude-ai's count, until the commander decided it (884bd56ece2a)."
    )


class HeldPlanFailure(PostmortemFailure):
    number = 23
    title = "It held plan work for hours while nothing reached main"
    evidence = (
        "October 21: every virtual bus step had a holder and none merged for 22 hours, because "
        "the steps only land together and nobody owned integrating them; Tristan caught it "
        "(claude-ai #comms 2dde16189509)."
    )


class KeptDeadCopyFailure(PostmortemFailure):
    number = 24
    title = "It kept a dead copy and called it a legacy case"
    evidence = (
        "October 21: claude-ai kept ten finished carry steps and a schema step as legacy cases "
        "although the one bus had run them all; their dry runs broke installs and tests until "
        "Tristan asked what legacy case there could be with zero users (#2578)."
    )


class NotedSlopFailure(PostmortemFailure):
    number = 25
    title = "It noted slop for later instead of deleting it"
    evidence = (
        "October 21: three-minute commander CLI calls sat on claude-ai's list as something to "
        "profile later; Tristan called it mega slop, and the profile showed the answer gate "
        "re-reading every directive (#2579)."
    )


class RestatedOwnerFailure(PostmortemFailure):
    number = 26
    title = "It restated an owner's rule in a second file"
    evidence = (
        "October 21: claude-ai added 'name slop as slop' to its own skill while the commander "
        "skill and indictment already owned it, and kept its habits in a file only it read."
    )


class GuessedOrderFailure(PostmortemFailure):
    number = 27
    title = "It gave an order from a guess about code it had not read"
    evidence = (
        "October 21: claude-ai told the commander one chokehold id answers the burst; the gate "
        "it had not read still required every line, and the answer was refused (#2573)."
    )


class WorkaroundAsFixFailure(PostmortemFailure):
    number = 28
    title = "It called a workaround a fix"
    evidence = (
        "October 21: claude-ai reported a workaround for a stalled root fix as the fix while "
        "the root fix had not landed and nothing showed its progress."
    )


class SoftenedSlopFailure(PostmortemFailure):
    number = 29
    title = "It softened slop into neutral words"
    evidence = (
        "October 21: claude-ai wrote legacy, kept for safety and paused where the true word was "
        "slop; Tristan: handwritten is slop, use the word."
    )


class PaddedWithChecksFailure(PostmortemFailure):
    number = 30
    title = "It waited on checks that test nothing it changed"
    evidence = (
        "October 21: claude-ai held diagnosed fixes about ten minutes per push for smoke runs "
        "that never ran the changed tests, and the tests it had broken stayed broken on main "
        "anyway (#2572)."
    )


@dataclass(frozen=True)
class CommanderRule(PatternSource):
    lead: str

    def cite(self) -> str:
        return f'[comms-commander rule]({COMMANDER_SKILL}) "{self.lead}"'


@dataclass(frozen=True)
class ProblemFix(PatternSource):
    problem: str
    commit: str

    def cite(self) -> str:
        return f"{self.problem} ({self.commit})"


@dataclass(frozen=True)
class SeedCatalog(PatternSource):
    """A pattern's seed ID in claude-ai's numbered catalog (message 1c8729ba0ed7)."""

    seed: int

    def cite(self) -> str:
        return f"seed catalog pattern {self.seed} (claude-ai message 1c8729ba0ed7)"


@dataclass(frozen=True)
class Commit(PatternSource):
    commit: str

    def cite(self) -> str:
        return f"commit {self.commit}"
