"""The commander's verdict on every unit edit, judged against the order that unit is carrying out.

A send-back records its quoted slop and replacement in the native verdict reason.
Core publishes that recorded reason once; this command adds no separate roast post.
--serves quotes the order Todo, which also owns every verdict.

A verdict is a set of semantic claims the tool can falsify: --serves must be words from
the unit's current order (which clause this edit carries out), and every edit must contain
one of the --line quotes (the deleted authority or added owner I judged it by). Without
reading each edit and deciding what it does, the claims cannot be written.

Each edit's CODE is printed only after its last hunk line, so a verdict needs the
whole edit on screen: a show piped into head or /dev/null yields no codes.

A verdict without a reason is refused; a send-back must cite a real message from
the commander to that unit. Nothing here judges code; it only refuses to let an edit pass unread.

`judge batch FILE` takes a JSON list of those same verbs, one object per verdict with
"kind" naming the verb, reads the unjudged edits once and records every verdict in one
call: every refused entry is listed together and nothing is written. `judge rewritten UNIT
--by COMMIT` settles each unjudged edit of UNIT none of whose added lines survives that
pushed commit; Core checks git for each, and edits it refuses stay for ok or back.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from typing import Annotated, ClassVar
from abc import abstractmethod
from dataclasses import asdict, dataclass
from functools import cached_property
from pathlib import Path


from agent_comms.comms import Comms
from agent_comms.change_report import FileLine
from agent_comms.field_codec import (
    Argument, BoundedText, FieldCodec, MalformedRecord, MessageId, RecordProblems,
    SelectingArgumentDefault,
)
from agent_comms.declared_family import DeclaredFamily
from agent_comms.head_review import PullRequestHead
from agent_comms.recorded_time import EventTime
from agent_comms.errors import TodoError
from agent_comms.repo import Repo
from agent_comms.todos import (
    AcceptedVerdict,
    BluntText,
    DirectiveText,
    DroppedVerdict,
    EditSendBackVerdict,
    PeerAcceptedVerdict,
    RewrittenVerdict,
    GrownHeadVerdict,
    GrowthReason,
    ReviewedHeadVerdict,
    RejectedHeadVerdict,
    HeadVerdict,
    HeadReviewOrder,
    JudgedCall,
    LinkedHeadReview,
    OrderLink,
    OrderQuote,
    SendBackVerdict,
    StrikeReason,
    SupersededVerdict,
    Todo,
    ToolCallVerdict,
    UnderReviewHeadVerdict,
    UnquotedHeadReview,
    VerdictReason,
)
from agent_comms.slop import Derivation, Slop
from agent_comms.order_tools import CommsHeadVerdictTool, CommsUnjudgedTool, CommsVerdictsTool
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tool_declaration import EditCall, ReplacementEdit
from agent_comms.commander import order
from agent_comms.commander.order import CommanderVerb

EDIT_LINE = "required with edit IDs: an exact diff line each edit contains; repeat for each"
BRANCH = re.compile(r"\bisinstance\(|\bhasattr\(|^\s*(if|elif|match|case)\b|\bif\b.*\belse\b")


class Hunk(ReplacementEdit):

    def branches_added(self) -> int:
        """Branches the new text has beyond the old text (branch-vs-polymorphism: presumed slop)."""
        return sum(1 for line in self.new_text.splitlines() if BRANCH.search(line)) - sum(
            1 for line in self.old_text.splitlines() if BRANCH.search(line))

    def rendered(self) -> str:
        return (''.join(f'- {line}\n' for line in self.old_text.splitlines())
                + ''.join(f'+ {line}\n' for line in self.new_text.splitlines()))


class Edit(EditCall):
    """The Core result row, decoded at the commander's tool boundary."""
    hunks: tuple[Hunk, ...]

    @classmethod
    def unjudged(cls, comms: Comms, commander: str) -> list[Edit]:
        reply = CommsUnjudgedTool.invoke(comms, {'thread': commander}, caller=ThreadCaller(commander))
        return list(FieldCodec.decode(tuple[cls, ...], reply['edits']))

    def code(self) -> str:
        """Four hex digits derived from the edit's own text; shown only after its full hunks."""
        return hashlib.sha256((self.id + json.dumps([asdict(h) for h in self.hunks])).encode()).hexdigest()[:4]

    def branches_added(self) -> int:
        return sum(hunk.branches_added() for hunk in self.hunks)

    def text(self) -> str:
        return ''.join(hunk.old_text + hunk.new_text for hunk in self.hunks)

    def added_lines(self) -> tuple[str, ...]:
        return tuple(line for hunk in self.hunks for line in hunk.new_text.splitlines())

    def added(self) -> str:
        return ''.join('+' + line + '\n' for line in self.added_lines())

    def rendered(self) -> str:
        return (f'--- {self.id} {self.at.clock()} {self.path}\n'
                + '\n'.join(hunk.rendered() for hunk in self.hunks)
                + f'\n=== end {self.id} code {self.code()}')


def show(ctx, pending: list[Edit]) -> None:
    by_thread: dict[str, list[Edit]] = {}
    for edit in pending:
        by_thread.setdefault(edit.thread, []).append(edit)
    for name, edits in by_thread.items():
        orders = ctx.held_by(name)
        intent = '\n'.join(todo.order.instructions(todo) for todo in orders) or 'NO OPEN ORDER TODO'
        print(f'===== {name}: {len(edits)} unjudged\nCURRENT ORDERS:\n{intent}\n')
        for edit in edits:
            print(edit.rendered())
    print(f'{len(pending)} unjudged')


def alarm(ctx):
    while True:
        late = [edit for edit in Edit.unjudged(ctx.comms, ctx.commander)
                if edit.at.before(time.time() - 300)]
        if late:
            counts: dict[str, int] = {}
            for edit in late:
                counts[edit.thread] = counts.get(edit.thread, 0) + 1
            oldest = min(edit.at.recorded_at("An overdue edit") for edit in late)
            print(f'UNJUDGED {len(late)} edits over 5 min (oldest {int((time.time() - oldest) / 60)} min): '
                  + ', '.join(f'{t} {n}' for t, n in counts.items()) + ' -> judge show', flush=True)
        time.sleep(60)


class JudgeVerb(CommanderVerb):
    """A verb of `judge`."""


@dataclass(frozen=True, kw_only=True)
class ShowVerb(JudgeVerb):
    help = "every unit's unjudged edits in full, beside its current orders"
    thread: Annotated[
        str, Argument(flags=("thread",), options={"nargs": "?"})
    ] = ""

    def selects(self, edit: Edit) -> bool:
        return not self.thread or edit.thread == self.thread

    def run(self, ctx: order.Commander) -> None:
        show(ctx, [edit for edit in Edit.unjudged(ctx.comms, ctx.commander) if self.selects(edit)])


class AlarmVerb(JudgeVerb):
    help = "loop: every minute, name edits unjudged for 5 min"

    def run(self, ctx: order.Commander) -> None:
        alarm(ctx)


class ReviewSubject(DeclaredFamily):
    """Decode the CLI's mutually exclusive selectors once, then keep the selected subject."""

    @classmethod
    def from_options(cls, options: HeadVerdictVerb, ctx: order.Commander) -> ReviewSubject:
        matches = tuple(member for member in cls.members_with(cls) if member.selected(options))
        if len(matches) != 1:
            sys.exit('Refused: select either edit IDs or --pr')
        member, = matches
        return member.decode_options(options, ctx)

    @classmethod
    @abstractmethod
    def selected(cls, options: HeadVerdictVerb) -> bool: ...

    @classmethod
    @abstractmethod
    def decode_options(cls, options: HeadVerdictVerb, ctx: order.Commander) -> ReviewSubject: ...

    @abstractmethod
    def run(self, options: VerdictVerb, ctx: order.Commander) -> None: ...

    @abstractmethod
    def verdicts(self, options: VerdictVerb, ctx: order.Commander,
                 unjudged: list[Edit]) -> tuple[JudgedCall, ...]: ...


@dataclass(frozen=True)
class EditReview(ReviewSubject):
    ids: tuple[str, ...]

    @classmethod
    def selected(cls, options: HeadVerdictVerb) -> bool:
        return bool(options.ids)

    @classmethod
    def decode_options(cls, options: HeadVerdictVerb, ctx: order.Commander) -> EditReview:
        if options.head or options.repo:
            sys.exit('Refused: --head and --repo belong to --pr, not edit IDs')
        return cls(options.ids)

    def run(self, options: VerdictVerb, ctx: order.Commander) -> None:
        unjudged = Edit.unjudged(ctx.comms, ctx.commander)
        record(ctx, options, self.verdicts(options, ctx, unjudged), len(unjudged))

    def verdicts(self, options: VerdictVerb, ctx: order.Commander,
                 unjudged: list[Edit]) -> tuple[JudgedCall, ...]:
        """The verdicts these IDs carry; a refusal exits before anything is written."""
        if not self.ids:
            sys.exit('Refused: select at least one edit ID:CODE')
        orders = {todo.id: todo for todo in ctx.held_orders()}
        edits = {edit.id: edit for edit in unjudged if edit.problem in orders}
        ids = [token.split(':')[0] for token in self.ids]
        problems = {token: 'unknown edit id' for token, i in zip(self.ids, ids) if i not in edits}
        known = [(token, edits[i]) for token, i in zip(self.ids, ids) if i in edits]
        problems |= {f'{token} --problem': f'its checkout files this edit under {edit.problem}'
                     for token, edit in known if options.problem and edit.problem != options.problem}
        problems |= {f'{token} code': 'wrong or missing read code; read the edit to its "=== end" line'
                     for token, edit in known if token != f'{edit.id}:{edit.code()}'}
        problems |= {f'{token} --line': ('none of your --line quotes is in this edit; judge it by its own text'
                                         if options.lines else EDIT_LINE)
                     for token, edit in known if not any(line in edit.text() for line in options.lines)}
        if problems:
            raise RecordProblems(problems)
        options.check_edits([edits[i] for i in ids])
        return tuple(JudgedCall(edits[i].problem,
                                options.operation(ctx, orders[edits[i].problem], edits[i]))
                     for i in ids)


def record(ctx: order.Commander, verb: JudgeVerb, verdicts: tuple[JudgedCall, ...], unjudged: int) -> None:
    """Core checks every verdict before writing any; the count left derives from the one read."""
    ctx.call(CommsVerdictsTool(verdicts=verdicts))
    print(f'{verb.declared_name}: {len(verdicts)} edits; {unjudged - len(verdicts)} still unjudged')


@dataclass(frozen=True)
class PullRequestReview(ReviewSubject):
    request: PullRequestHead

    @classmethod
    def selected(cls, options: HeadVerdictVerb) -> bool:
        return bool(options.pr)

    @classmethod
    def decode_options(cls, options: HeadVerdictVerb, ctx: order.Commander) -> PullRequestReview:
        return cls(PullRequestHead.among(
            tuple(repository.github for repository in Repo.candidates(ctx.repos, options.repo)),
            options.pr, options.head))

    def run(self, options: HeadVerdictVerb, ctx: order.Commander) -> None:
        options.check_edits([])
        linked = HeadReviewOrder.for_request(ctx.held_orders(), self.request, options.order_link,
                                            options.problem)
        ctx.call(CommsHeadVerdictTool(request=self.request,
                                    operation=options.head_operation(), order=linked))
        print(f'PR #{self.request.pr} head {self.request.head}: {options.declared_name}')

    def verdicts(self, options: VerdictVerb, ctx: order.Commander,
                 unjudged: list[Edit]) -> tuple[JudgedCall, ...]:
        sys.exit('Refused: a batch judges edit IDs; review a PR head with its own call')


class WordedText(BoundedText):
    """Free text that must say at least its number of words. FieldCodec decodes every option
    of a verb at once, so every short text is refused in the same refusal."""

    words: ClassVar[int]
    says: ClassVar[str]

    @classmethod
    def from_text(cls, value: str) -> str:
        if len(super().from_text(value).split()) < cls.words:
            raise MalformedRecord(f"{cls.says} ({cls.words}+ words)")
        return value


class ReasonText(BoundedText):
    """VerdictReason owns how many words a reason takes and which words it may not use."""

    @classmethod
    def from_text(cls, value: str) -> str:
        if refused := VerdictReason.wording_refusal(super().from_text(value)):
            raise MalformedRecord(" ".join(refused))
        return value


class RoastText(WordedText):
    words = 12
    says = "name the duplicated fact and its owner, with because"

    @classmethod
    def from_text(cls, value: str) -> str:
        if not re.search(r'\bbecause\b', super().from_text(value)):
            raise MalformedRecord(f"{cls.says} ({cls.words}+ words)")
        return value


class FixText(WordedText):
    words = 8
    says = "name the owner and the replacing shape"


@dataclass(frozen=True, kw_only=True)
class ReasonedVerb(JudgeVerb, declared_name=None):
    """A verb whose verdict records why: what the edit serves, violates or was replaced by."""
    why: Annotated[str, ReasonText]




@dataclass(frozen=True, kw_only=True)
class VerdictVerb(JudgeVerb):
    """Every verdict, mine or a fork's, is made after reviewing the OOPSLA paper's
    correct-maintenance results. Each member's required options are the facts its verdict carries."""

    ids: Annotated[
        tuple[str, ...],
        Argument(flags=("ids",), options={"nargs": "*", "metavar": "ID:CODE"}),
    ] = ()
    problem: Annotated[
        str,
        Argument(options={"metavar": "PROBLEM", "help": "the held problem id from order board"}),
    ] = ""
    lines: Annotated[
        tuple[str, ...],
        Argument(
            flags=("--line",),
            options={
                "action": "append",
                "metavar": "LINE",
                "help": EDIT_LINE,
            },
        ),
    ] = ()

    @abstractmethod
    def operation(self, ctx: order.Commander, todo: Todo, edit: Edit) -> ToolCallVerdict: ...

    @abstractmethod
    def check_edits(self, edits: list[Edit]) -> None: ...

    def subject(self, ctx: order.Commander) -> ReviewSubject:
        return EditReview(self.ids)

    def run(self, ctx: order.Commander) -> None:
        self.subject(ctx).run(self, ctx)

    def refused(self, number: int, code: object) -> str:
        """One batch entry's refusal, named by its own verb."""
        return f'entry {number} ({self.declared_name}): {code}'


@dataclass(frozen=True, kw_only=True)
class HeadVerdictVerb(VerdictVerb, declared_name=None):
    pr: Annotated[int, Argument(options={"help": "review this PR instead of individual edits"})] = 0
    head: Annotated[str, Argument(options={"help": "the exact reviewed SHA; required with --pr"})] = ""
    repo: Annotated[str, Argument(options={
        "help": "registered repository name; without it, the one whose --pr is at --head"})] = ""

    def subject(self, ctx: order.Commander) -> ReviewSubject:
        return ReviewSubject.from_options(self, ctx)

    def order_link(self, todo: Todo) -> OrderLink:
        """Reading or rejecting a head carries out no clause of the order it is linked to."""
        return UnquotedHeadReview.of(todo, self.head_evidence())

    def head_evidence(self) -> tuple[FileLine, ...]:
        return ()

    @abstractmethod
    def head_operation(self) -> HeadVerdict: ...


@dataclass(frozen=True, kw_only=True)
class QuotingVerb(HeadVerdictVerb, declared_name=None):
    """A verb whose verdict stores the words of the order it carries out."""
    serves: Annotated[str, Argument(options={"help": OrderQuote.rule})] = ""

    def order_link(self, todo: Todo) -> OrderLink:
        return LinkedHeadReview.of(todo, self.serves, self.head_evidence())


@dataclass(frozen=True, kw_only=True)
class OkVerb(ReasonedVerb, QuotingVerb):
    help = "the edit or exact PR head is accepted"

    def head_operation(self) -> HeadVerdict:
        reason = VerdictReason(self.why)
        return (GrownHeadVerdict(reason, GrowthReason.every(self.growth)) if self.growth
                else ReviewedHeadVerdict(reason))
    growth: Annotated[
        tuple[str, ...],
        Argument(options={"action": "append", "metavar": "REASON",
                          "help": "12+ words naming a growing src file and its need; repeat for each"}),
    ] = ()
    no_owner: Annotated[
        str,
        Argument(
            flags=("--no-owner",), options={"help": AcceptedVerdict.branch_rule}
        ),
    ] = ""
    peer: Annotated[
        str,
        Argument(
            flags=("--peer",),
            options={"help": "another sender's retained order message to the unit"},
        ),
    ] = ""

    def operation(self, ctx: order.Commander, todo: Todo, edit: Edit) -> AcceptedVerdict:
        actor = ctx.actor_identity
        if self.peer:
            return PeerAcceptedVerdict(
                todo.revision, actor, todo.held(), edit.id,
                quote=self.serves, peer=FieldCodec.decode(MessageId, self.peer),
            )
        return AcceptedVerdict(todo.revision, actor, todo.held(), edit.id, quote=self.serves)

    def check_edits(self, edits: list[Edit]) -> None:
        for edit in edits:
            path = edit.path
            after = [open(path).read()] if path.endswith('.py') and os.path.exists(path) else []
            if added := order.forwarders_added(edit.added(), after):
                sys.exit(f'Refused: edit {edit.id} adds forwarders {added[:5]}; it goes back with judge back, never ok')
            AcceptedVerdict.require_branches(edit.branches_added(), self.no_owner)

@dataclass(frozen=True, kw_only=True)
class ReviewingVerb(HeadVerdictVerb):
    help = "a fork is reading this exact PR head; its delivery is not owed until ok or back"

    def head_operation(self) -> HeadVerdict:
        return UnderReviewHeadVerdict()

    def operation(self, ctx: order.Commander, todo: Todo, edit: Edit) -> ToolCallVerdict:
        sys.exit('Refused: judge reviewing takes --pr and --head, never edit ids.')

    def check_edits(self, edits: list[Edit]) -> None:
        if edits:
            sys.exit('Refused: judge reviewing takes --pr and --head, never edit ids.')


@dataclass(frozen=True, kw_only=True)
class SupersededVerb(ReasonedVerb, VerdictVerb):
    help = "the task moved to another unit; these edits are neither ok nor sent back"
    by: Annotated[
        str,
        Argument(
            flags=("--by",),
            options={"metavar": "PROBLEM", "help": "the replacing problem id from order board"},
        ),
    ]

    def operation(self, ctx: order.Commander, todo: Todo, edit: Edit) -> SupersededVerdict:
        return SupersededVerdict(todo.revision,
                                 ctx.actor_identity,
                                 todo.held(), edit.id, replacement=self.by, reason=self.why)

    def check_edits(self, edits: list[Edit]) -> None:
        pass

@dataclass(frozen=True, kw_only=True)
class DroppedVerb(VerdictVerb):
    help = "the unit discarded these edits; Core settles them only if git holds them on no ref"

    def operation(self, ctx: order.Commander, todo: Todo, edit: Edit) -> DroppedVerdict:
        return DroppedVerdict(todo.revision, ctx.actor_identity, todo.held(), edit.id,
                              path=edit.path, added=edit.added_lines())

    def check_edits(self, edits: list[Edit]) -> None:
        pass

@dataclass(frozen=True, kw_only=True)
class BackVerb(ReasonedVerb, HeadVerdictVerb):
    """A correctness send-back: the head is broken at a line, whether or not it restates a fact."""
    help = ("reject the edit or exact PR head; Core records its reason once. With --slop and its "
            "statement it is a slop send-back, without them a correctness one")
    roast: Annotated[
        str, RoastText, Argument(options={"help": "blunt, why it is stupid, with because"})
    ]
    fix: Annotated[str, FixText, Argument(options={"help": "owner and replacing shape"})]
    at: Annotated[
        str, Argument(options={"help": "PATH:LINE of the failing path, or where the slop restates its fact"})
    ]

    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        """One `back` takes every member's options; the options given choose the member."""
        FieldCodec.add_arguments(cls.widest(), subparsers.add_parser(cls.declared_name, help=cls.help))

    @classmethod
    def from_arguments(cls, args: argparse.Namespace) -> BackVerb:
        """The narrowest member holding every option given decodes them, and lists every
        option of its own still missing."""
        given = vars(args).keys() & cls.options_of(cls.widest())
        return FieldCodec.decode_arguments(min(
            (member for member in cls.members_with(cls) if given <= cls.options_of(member)),
            key=lambda member: len(cls.options_of(member))), args)

    @classmethod
    def widest(cls) -> type[BackVerb]:
        return max(cls.members_with(cls), key=lambda member: len(cls.options_of(member)))

    @staticmethod
    def options_of(member: type) -> set[str]:
        return {entry.key for entry in FieldCodec.argument_fields(member)}

    def defect(self) -> str:
        return f"The failing path is {self.at}."

    @cached_property
    def rejection_reason(self) -> StrikeReason:
        order.refuse(self.roast, BluntText)
        order.refuse(' '.join((self.why, self.roast, self.fix)), *DirectiveText.checks())
        return StrikeReason("\n".join((
            self.why, self.roast,
            *(f"    {line.strip()}" for line in self.lines),
            self.defect(), f"What replaces it: {self.fix}",
        )))

    def head_evidence(self) -> tuple[FileLine, ...]:
        return (FileLine.from_wire(self.at),)

    def head_operation(self) -> HeadVerdict:
        return RejectedHeadVerdict(self.rejection_reason)

    @cached_property
    def judged_at(self) -> EventTime:
        """One back call is one send-back: every edit it judges shares this moment, and the
        strike it records repeats only for verdicts of the same call."""
        return EventTime.capture(time.time())

    def operation(self, ctx: order.Commander, todo: Todo, edit: Edit) -> SendBackVerdict:
        return EditSendBackVerdict(todo.revision, ctx.actor_identity,
                                   todo.held(), edit.id, reason=self.rejection_reason,
                                   evidence=self.head_evidence(), path=edit.path,
                                   at=self.judged_at)

    def check_edits(self, edits: list[Edit]) -> None:
        _ = self.rejection_reason

@dataclass(frozen=True, kw_only=True)
class SlopBackVerb(BackVerb):
    """A slop send-back: the defect is a fact restated outside its declaration, so it carries
    the formal statement. `back` parses its options; it has no subcommand of its own."""
    pattern: Annotated[str, Argument(flags=("--slop",), default=SelectingArgumentDefault(), options={
        "help": "slop only: pattern ID from the pinned refactor-audit catalog"})]
    fact: Annotated[str, Argument(default=SelectingArgumentDefault(), options={
        "help": "slop only: the required fact, 4+ words"})]
    derive: Annotated[str, Argument(default=SelectingArgumentDefault(), options={
        "help": "slop only: " + "|".join(Derivation.names())})]
    by: Annotated[str, Argument(default=SelectingArgumentDefault(), options={
        "help": "slop only: PATH:LINE of the declaration that owns it"})]

    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        """`back` already takes these options."""

    @cached_property
    def slop(self) -> Slop:
        return FieldCodec.decode(Slop, dict(pattern=self.pattern, fact=self.fact,
                                           at=self.at, derive=self.derive, by=self.by))

    def defect(self) -> str:
        return self.slop.render()


@dataclass(frozen=True, kw_only=True)
class BatchVerb(JudgeVerb):
    help = ('judge many edits from one JSON file: a list of ok, back, superseded and dropped '
            'verdicts, each with the fields its verb takes ({"kind": "ok", "ids": ["ID:CODE"], ...})')
    file: Annotated[Path, Argument(flags=("file",), options={"metavar": "FILE"})]

    def run(self, ctx: order.Commander) -> None:
        entries = FieldCodec.decode(tuple[VerdictVerb, ...], FieldCodec.parse(self.file.read_bytes()))
        unjudged = Edit.unjudged(ctx.comms, ctx.commander)
        verdicts: list[JudgedCall] = []
        refusals: list[str] = []
        for number, entry in enumerate(entries, 1):
            try:
                verdicts += entry.subject(ctx).verdicts(entry, ctx, unjudged)
            except SystemExit as refused:
                refusals.append(entry.refused(number, refused.code))
            except RecordProblems as refused:
                refusals.append(entry.refused(number, refused))
        judged = [call.operation.tool_call_id for call in verdicts]
        if twice := sorted({i for i in judged if judged.count(i) > 1}):
            refusals.append(f'edits judged twice in this batch: {twice}')
        if refusals:
            sys.exit('\n'.join(refusals))
        record(ctx, self, tuple(verdicts), len(unjudged))


@dataclass(frozen=True, kw_only=True)
class RewrittenVerb(ReasonedVerb):
    help = ("settle every unjudged edit of UNIT that a later pushed commit rewrote: Core checks "
            "each against git, and edits with a surviving line stay for ok or back")
    unit: Annotated[str, Argument(flags=("unit",), options={"metavar": "UNIT"})]
    by: Annotated[
        str, Argument(flags=("--by",), options={"metavar": "COMMIT", "help": "the later pushed commit"})
    ]

    def run(self, ctx: order.Commander) -> None:
        orders = {todo.id: todo for todo in ctx.held_by(self.unit)}
        unjudged = Edit.unjudged(ctx.comms, ctx.commander)
        verdicts: list[JudgedCall] = []
        live: dict[str, str] = {}
        for edit in (edit for edit in unjudged if edit.problem in orders):
            todo = orders[edit.problem]
            verdict = RewrittenVerdict(todo.revision, ctx.actor_identity, todo.held(), edit.id,
                                       path=edit.path, added=edit.added_lines(), commit=self.by,
                                       reason=self.why)
            try:
                verdict.prepare(ctx.comms.goals, todo)
            except TodoError as refused:
                live[edit.id] = str(refused).replace(edit.id, 'the edit')
                continue
            verdicts.append(JudgedCall(edit.problem, verdict))
        if verdicts:
            record(ctx, self, tuple(verdicts), len(unjudged))
        for reason in dict.fromkeys(live.values()):
            print(f'{reason}: ' + ' '.join(i for i, why in live.items() if why == reason))
        print(f'{len(live)} edits of {self.unit} stay for ok or back: ' + ' '.join(live))


def main(args, ctx):
    JudgeVerb.parse(args or ['show']).apply(ctx)
