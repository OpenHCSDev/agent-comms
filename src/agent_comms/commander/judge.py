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
"""

import hashlib
import json
import os
import re
import sys
import time
from typing import Annotated
from abc import abstractmethod
from dataclasses import asdict, dataclass, field


from agent_comms.comms import Comms
from agent_comms.change_report import FileLine
from agent_comms.field_codec import Argument, FieldCodec
from agent_comms.todos import (
    AcceptedVerdict,
    BluntText,
    Derivation,
    DroppedVerdict,
    PeerAcceptedVerdict,
    ReviewedHeadVerdict,
    SendBackVerdict,
    Slop,
    StrikeReason,
    SupersededVerdict,
    Todo,
    ToolCallVerdict,
)
from agent_comms.tools import CommsHeadVerdictTool, CommsUnjudgedTool, CommsVerdictTool
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tool_declaration import EditCall, ReplacementEdit
from agent_comms.commander import order
from agent_comms.commander.order import CommanderVerb

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

    def added(self) -> str:
        return ''.join('+' + line + '\n' for hunk in self.hunks for line in hunk.new_text.splitlines())

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


@dataclass(frozen=True, kw_only=True)
class HeadVerb(JudgeVerb):
    help = ("accept an order's reviewed PR head: every edit before it is judged and "
            "agent-comms/review is posted on the commit")
    problem: Annotated[str, Argument(flags=("problem",), options={"metavar": "PROBLEM"})]
    head: Annotated[str, Argument(flags=("head",), options={"metavar": "SHA"})]
    serves: Annotated[
        str, Argument(options={"help": "exact words of the order the head carries out"})
    ]

    def run(self, ctx: order.Commander) -> None:
        held = [todo for todo in ctx.held_orders() if todo.id == self.problem]
        if not held:
            sys.exit(f'Refused: {self.problem} is not an order this commander holds')
        todo, = held
        verdict = ReviewedHeadVerdict(todo.revision, ctx.actor_identity, todo.held(),
                                      quote=self.serves, head=self.head)
        ctx.call(CommsHeadVerdictTool(problem=todo.id, operation=verdict))
        left = len(Edit.unjudged(ctx.comms, ctx.commander))
        print(f'head {self.head} accepted on {todo.id}; {left} still unjudged')


@dataclass(frozen=True, kw_only=True)
class VerdictVerb(JudgeVerb):
    """Every verdict, mine or a fork's, is made after reviewing the OOPSLA paper's
    correct-maintenance results. Each member's required options are the facts its verdict carries."""

    ids: Annotated[
        tuple[str, ...],
        Argument(flags=("ids",), options={"nargs": "+", "metavar": "ID:CODE"}),
    ]
    why: str
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
                "help": "an exact diff line; repeat for each",
            },
        ),
    ]

    @abstractmethod
    def operation(self, ctx: order.Commander, todo: Todo, edit: Edit) -> ToolCallVerdict: ...

    @abstractmethod
    def check_edits(self, edits: list[Edit]) -> None: ...

    def run(self, ctx: order.Commander) -> None:
        if len(self.why.split()) < 6:
            sys.exit('Refused: state which intent the edit serves or violates, and why (6+ words).')
        orders = {todo.id: todo for todo in ctx.held_orders()
                  if not self.problem or todo.id == self.problem}
        edits = {edit.id: edit for edit in Edit.unjudged(ctx.comms, ctx.commander)
                 if edit.problem in orders}
        ids = [token.split(':')[0] for token in self.ids]
        if missing := [i for i in ids if i not in edits]:
            sys.exit(f'Refused: unknown edit ids {missing}')
        if unread := [token for token in self.ids
                      if token != f'{token.split(":")[0]}:{edits[token.split(":")[0]].code()}']:
            sys.exit(f'Refused: wrong or missing read codes {unread}; read each edit to its "=== end" line')
        for i in ids:
            if not any(line in edits[i].text() for line in self.lines):
                sys.exit(f'Refused: none of your --line quotes is in edit {i}; judge it by its own text')
        selected = {i: orders[edits[i].problem] for i in ids}
        self.check_edits([edits[i] for i in ids])
        # Validate the whole batch through the operation owner before its first write.
        for i, todo in selected.items():
            self.operation(ctx, todo, edits[i]).prepare(ctx.comms.goals, todo).apply(todo)
        current = {todo.id: todo for todo in selected.values()}
        for i, todo in selected.items():
            result = ctx.call(CommsVerdictTool(problem=todo.id,
                              operation=self.operation(ctx, current[todo.id], edits[i])))
            current[todo.id] = result.todo
        left = len(Edit.unjudged(ctx.comms, ctx.commander))
        print(f'{self.declared_name}: {len(ids)} edits; {left} still unjudged')


@dataclass(frozen=True, kw_only=True)
class OkVerb(VerdictVerb):
    help = "the edit serves the order"
    serves: Annotated[
        str, Argument(options={"help": "exact words of the order"})
    ]
    no_owner: Annotated[
        str,
        Argument(
            flags=("--no-owner",), options={"help": "12+ words with because and absolute file:line"}
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
            return PeerAcceptedVerdict(todo.revision, actor, todo.held(), edit.id,
                                       quote=self.serves, peer=self.peer)
        return AcceptedVerdict(todo.revision, actor, todo.held(), edit.id, quote=self.serves)

    def check_edits(self, edits: list[Edit]) -> None:
        for edit in edits:
            path = edit.path
            after = [open(path).read()] if path.endswith('.py') and os.path.exists(path) else []
            if added := order.forwarders_added(edit.added(), after):
                sys.exit(f'Refused: edit {edit.id} adds forwarders {added[:5]}; it goes back with judge back, never ok')
            AcceptedVerdict.require_branches(edit.branches_added(), self.no_owner)

@dataclass(frozen=True, kw_only=True)
class SupersededVerb(VerdictVerb):
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
        added = tuple(line for hunk in edit.hunks for line in hunk.new_text.splitlines())
        return DroppedVerdict(todo.revision, ctx.actor_identity, todo.held(), edit.id,
                              path=edit.path, added=added)

    def check_edits(self, edits: list[Edit]) -> None:
        pass

@dataclass(frozen=True, kw_only=True)
class BackVerb(VerdictVerb):
    help = "send the edit back; Core publishes the recorded reason once"
    # A bare annotation would inherit VerdictVerb's default; field() keeps it required.
    problem: Annotated[
        str,
        Argument(options={"metavar": "PROBLEM", "help": "required problem id from order board"}),
    ] = field()
    roast: Annotated[
        str, Argument(options={"help": "blunt, why it is stupid, with because"})
    ]
    fix: Annotated[str, Argument(options={"help": "owner and replacing shape"})]
    pattern: Annotated[
        str,
        Argument(
            flags=("--slop",), options={"help": "pattern ID from the pinned refactor-audit catalog"}
        ),
    ]
    fact: Annotated[
        str, Argument(options={"help": "the required fact, 4+ words"})
    ]
    at: Annotated[
        str, Argument(options={"help": "PATH:LINE where it is restated"})
    ]
    derive: Annotated[
        str, Argument(options={"help": "|".join(Derivation.names())})
    ]
    by: Annotated[
        str, Argument(options={"help": "PATH:LINE of the declaration that owns it"})
    ]

    def slop(self) -> Slop:
        return FieldCodec.decode(Slop, dict(pattern=self.pattern, fact=self.fact,
                                           at=self.at, derive=self.derive, by=self.by))

    def operation(self, ctx: order.Commander, todo: Todo, edit: Edit) -> SendBackVerdict:
        return SendBackVerdict(todo.revision, ctx.actor_identity,
                               todo.held(), edit.id,
                               reason=StrikeReason("\n".join((
                                   self.why, self.roast,
                                   *(f"    {line.strip()}" for line in self.lines),
                                   self.slop().render(), f"What replaces it: {self.fix}",
                               ))),
                               evidence=(FileLine.from_wire(self.at),))

    def check_edits(self, edits: list[Edit]) -> None:
        if len(self.roast.split()) < 12 or not re.search(r'\bbecause\b', self.roast):
            sys.exit('Refused: --roast needs 12+ words with because, naming the duplicated fact and its owner')
        if len(self.fix.split()) < 8:
            sys.exit('Refused: --fix needs 8+ words naming the owner and replacing shape')
        self.slop()
        order.refuse(self.roast, BluntText)
        order.refuse(' '.join((self.why, self.roast, self.fix)), *order.OUTGOING)

def main(args, ctx):
    JudgeVerb.parse(args or ['show']).apply(ctx)
