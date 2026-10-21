"""Review the current commit of a pull request linked to a held problem.

The Todo owns the verdict; HeadVerdict posts its GitHub status on that exact commit.
A new push has no verdict. Edit IDs and read codes are evidence, not review identities.
Every --line quote is checked against the linked request's diff at the selected head.
"""

import hashlib
import json
import re
import sys
import time
from abc import abstractmethod
from dataclasses import asdict, dataclass
from typing import Annotated

from agent_comms.comms import Comms
from agent_comms.change_report import FileLine
from agent_comms.field_codec import Argument, FieldCodec
from agent_comms.todos import (
    AcceptedHeadVerdict,
    BluntText,
    Derivation,
    HeadVerdict,
    ListedPullRequest,
    PullRequest,
    PullRequests,
    RejectedHeadVerdict,
    Slop,
    StrikeReason,
    Todo,
    UnmergedListedPullRequest,
)
from agent_comms.tools import CommsUnjudgedTool, CommsVerdictTool
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tool_declaration import EditCall, ReplacementEdit
from agent_comms.commander import order
from agent_comms.commander.order import CommanderVerb


class Hunk(ReplacementEdit):
    def rendered(self) -> str:
        return (''.join(f'- {line}\n' for line in self.old_text.splitlines())
                + ''.join(f'+ {line}\n' for line in self.new_text.splitlines()))


class Edit(EditCall):
    """Original edit evidence, decoded at the commander's tool boundary."""
    hunks: tuple[Hunk, ...]

    @classmethod
    def unjudged(cls, comms: Comms, commander: str) -> list[Edit]:
        reply = CommsUnjudgedTool.invoke(comms, {'thread': commander}, caller=ThreadCaller(commander))
        return list(FieldCodec.decode(tuple[cls, ...], reply['edits']))

    def code(self) -> str:
        return hashlib.sha256((self.id + json.dumps([asdict(h) for h in self.hunks])).encode()).hexdigest()[:4]

    def rendered(self) -> str:
        return (f'--- {self.id} {self.at.clock()} {self.path}\n'
                + '\n'.join(hunk.rendered() for hunk in self.hunks)
                + f'\n=== end {self.id} code {self.code()}')


def show(ctx: order.Commander, pending: list[Edit], held: tuple[Todo, ...]) -> None:
    pulls = PullRequests(held)
    for todo in held:
        print(f'===== {todo.id} {todo.holder}\n{todo.order.instructions(todo)}')
        for message in todo.messages[-1:]:
            print(f'Sent: {message.message_id}')
        requests = pulls.listed(todo.repo, ListedPullRequest, todo)
        if not requests:
            print('No linked pull request; publish the working branch first.')
        for request in requests:
            verdict = todo.head_verdict(request.head)
            print(f'PR #{request.number} head {request.head}: '
                  + (verdict.line if verdict is not None else 'unreviewed'))
        evidence = [edit for edit in pending if edit.problem == todo.id]
        print(f'{len(evidence)} edits since the last head verdict:')
        for edit in evidence:
            print(edit.rendered())


def roast(ctx, audience, thread, lines, text, fix, slop):
    """Keep the rejected code, duplicated fact and replacement visible to its audience."""
    body = (f'{thread}, this is stupid slop and it is going back:\n'
            + '\n'.join(f'    {line.strip()}' for line in lines)
            + f'\nThe slop: {slop.render()}'
            + f'\nWhy it is stupid: {text}\nWhat replaces it: {fix}\nNobody writes this again.')
    print(f'roast sent to {", ".join(audience.threads)}: ' + ctx.post(audience, body))


class JudgeVerb(CommanderVerb):
    """A verb of `judge`; judgment is never gated by unrelated order refusals."""


@dataclass(frozen=True, kw_only=True)
class ShowVerb(JudgeVerb):
    help = "linked pull request heads, their verdicts, and original edit evidence"
    thread: Annotated[str, Argument(flags=("thread",), options={"nargs": "?"})] = ""

    def run(self, ctx: order.Commander) -> None:
        held = tuple(todo for todo in ctx.held_orders()
                     if not self.thread or todo.holder == self.thread)
        show(ctx, Edit.unjudged(ctx.comms, ctx.commander), held)


@dataclass(frozen=True, kw_only=True)
class VerdictVerb(JudgeVerb):
    problem: Annotated[str, Argument(flags=("problem",), options={"metavar": "PROBLEM"})]
    why: str
    serves: Annotated[str, Argument(options={"help": "exact words of the order"})]
    lines: Annotated[
        tuple[str, ...],
        Argument(flags=("--line",), options={"action": "append", "metavar": "LINE",
                                            "help": "an exact line of the PR diff; repeat"}),
    ]

    @abstractmethod
    def operation(self, ctx: order.Commander, todo: Todo, request: PullRequest) -> HeadVerdict: ...

    def check_diff(self, diff: str) -> None:
        if len(self.why.split()) < 6:
            sys.exit('Refused: say which intent the head serves or violates and why (6+ words).')
        if not self.lines or any(line not in diff for line in self.lines):
            sys.exit('Refused: every --line must quote the linked PR diff at its current head.')

    def report(self, ctx: order.Commander, todo: Todo) -> None:
        """Acceptance has no additional public effect beyond the recorded review status."""

    def run(self, ctx: order.Commander) -> None:
        todo = next((todo for todo in ctx.held_orders() if todo.id == self.problem), None)
        if todo is None:
            sys.exit(f'Refused: {self.problem} is not a held order of this commander.')
        requests = PullRequests((todo,)).listed(todo.repo, UnmergedListedPullRequest, todo)
        if len(requests) != 1:
            sys.exit(f'Refused: {todo.id} must link exactly one open pull request; found {len(requests)}.')
        request, = requests
        self.check_diff(request.diff(todo.repo))
        result = ctx.call(CommsVerdictTool(problem=todo.id,
                          operation=self.operation(ctx, todo, request)))
        print(f'{self.declared_name}: {todo.id} PR #{request.number} head {request.head}')
        self.report(ctx, result.todo)


@dataclass(frozen=True, kw_only=True)
class OkVerb(VerdictVerb):
    help = "accept the linked pull request's current head"

    def operation(self, ctx: order.Commander, todo: Todo, request: PullRequest) -> HeadVerdict:
        return AcceptedHeadVerdict(todo.revision, ctx.incarnation, todo.held(),
                                   quote=self.serves, head=request.head)


@dataclass(frozen=True, kw_only=True)
class BackVerb(VerdictVerb):
    help = "reject the linked current head, record its strike and post the roast"
    roast: Annotated[str, Argument(options={"help": "blunt, why it is stupid, with because"})]
    fix: Annotated[str, Argument(options={"help": "owner and replacing shape"})]
    pattern: Annotated[
        str, Argument(flags=("--slop",), options={"help": "refactor-audit pattern ID"}),
    ]
    fact: Annotated[str, Argument(options={"help": "the required fact, 4+ words"})]
    at: Annotated[str, Argument(options={"help": "PATH:LINE where it is restated"})]
    derive: Annotated[str, Argument(options={"help": "|".join(Derivation.names())})]
    by: Annotated[str, Argument(options={"help": "PATH:LINE of its owning declaration"})]

    def slop(self) -> Slop:
        return FieldCodec.decode(Slop, dict(pattern=self.pattern, fact=self.fact,
                                           at=self.at, derive=self.derive, by=self.by))

    def operation(self, ctx: order.Commander, todo: Todo, request: PullRequest) -> HeadVerdict:
        return RejectedHeadVerdict(todo.revision, ctx.incarnation, todo.held(),
            quote=self.serves, head=request.head,
            reason=StrikeReason(f'{todo.id} head sent back: {self.why}'),
            evidence=(FileLine.from_wire(self.at),))

    def check_diff(self, diff: str) -> None:
        super().check_diff(diff)
        if len(self.roast.split()) < 12 or not re.search(r'\bbecause\b', self.roast):
            sys.exit('Refused: --roast needs 12+ words with because, naming the duplicated fact and its owner')
        if len(self.fix.split()) < 8:
            sys.exit('Refused: --fix needs 8+ words naming the owner and replacing shape')
        self.slop()
        order.refuse(self.roast, BluntText)
        order.refuse(' '.join((self.why, self.roast, self.fix)), *order.OUTGOING)

    def report(self, ctx: order.Commander, todo: Todo) -> None:
        verdict = todo.verdicts[-1]
        holder = verdict.previous.owner
        audience = verdict.audience.of(holder, todo.goal)
        roast(ctx, audience, holder, self.lines, self.roast, self.fix, self.slop())
        line = (f'{time.strftime("%Y-%m-%d %H:%M")} {holder} '
                f'(strike {todo.strike_count} on {todo.id}; caught by {ctx.commander} at review): '
                f'{todo.strikes[-1].reason}')
        ctx.on_record(line, 'Send-back', audience)


def main(args, ctx):
    JudgeVerb.parse(args or ['show']).apply(ctx)
