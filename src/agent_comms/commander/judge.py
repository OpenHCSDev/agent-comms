"""The commander's verdict on every unit edit, judged against the order that unit is carrying out.

A send-back's --roast is posted to its verdict's audience with the quoted slop line, so the
unit learns what was stupid and nobody repeats it; a send-back without a roast is refused.
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
import hashlib, json, os, re, sys, time
from abc import abstractmethod
from dataclasses import asdict, dataclass

from ..comms import Comms
from ..todos import AcceptedVerdict, BluntText, OrderVerdict, SendBackVerdict, StrikeReason, Todo, Slop, SlopKind, Derivation, SupersededVerdict
from ..tools import CommsUnjudgedTool, CommsVerdictTool
from ..field_codec import FieldCodec
from ..tool_caller import ThreadCaller
from ..tool_declaration import EditCall, ReplacementEdit
from . import order
from ..cli_commands import option
from .order import CommanderVerb, RuledVerb
from .config import CommanderConfig

CONFIG = CommanderConfig.installed()
ME = CONFIG.commander


BRANCH = re.compile(r'\bisinstance\(|\bhasattr\(|^\s*(if|elif|match|case)\b|\bif\b.*\belse\b')


@dataclass(frozen=True)
class Hunk(ReplacementEdit):

    def branches_added(self) -> int:
        """Branches the new text has beyond the old text (branch-vs-polymorphism: presumed slop)."""
        return sum(1 for line in self.new_text.splitlines() if BRANCH.search(line)) - sum(
            1 for line in self.old_text.splitlines() if BRANCH.search(line))

    def rendered(self) -> str:
        return (''.join(f'- {line}\n' for line in self.old_text.splitlines())
                + ''.join(f'+ {line}\n' for line in self.new_text.splitlines()))


@dataclass(frozen=True)
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
        stamp = time.strftime('%H:%M', time.localtime(self.at))
        return (f'--- {self.id} {stamp} {self.path}\n'
                + '\n'.join(hunk.rendered() for hunk in self.hunks)
                + f'\n=== end {self.id} code {self.code()}')


def show(pending: list[Edit]) -> None:
    by_thread: dict[str, list[Edit]] = {}
    for edit in pending:
        by_thread.setdefault(edit.thread, []).append(edit)
    for name, edits in by_thread.items():
        orders = order.held_by(name)
        intent = '\n'.join(todo.order.instructions(todo) for todo in orders) or 'NO OPEN ORDER TODO'
        print(f'===== {name}: {len(edits)} unjudged\nCURRENT ORDERS:\n{intent}\n')
        for edit in edits:
            print(edit.rendered())
    print(f'{len(pending)} unjudged')


def roast(audience, thread, lines, text, fix, slop):
    """Every send-back is a lesson, sent to its verdict's audience: the quoted slop,
    the required fact it restates and the declaration that derives it (paper 1's definition), why that is
    stupid, and what replaces it."""
    body = (f'{thread}, this is stupid slop and it is going back:\n'
            + '\n'.join(f'    {l.strip()}' for l in lines)
            + f'\nThe slop: {slop.render()}'
            + f'\nWhy it is stupid: {text}\nWhat replaces it: {fix}\nNobody writes this again.')
    print(f'roast sent to {", ".join(audience.threads)}: ' + order.post(audience, body))


def alarm():
    while True:
        late = [edit for edit in Edit.unjudged(order.COMMS, ME) if time.time() - edit.at > 300]
        if late:
            counts: dict[str, int] = {}
            for edit in late:
                counts[edit.thread] = counts.get(edit.thread, 0) + 1
            oldest = min(edit.at for edit in late)
            print(f'UNJUDGED {len(late)} edits over 5 min (oldest {int((time.time() - oldest) / 60)} min): '
                  + ', '.join(f'{t} {n}' for t, n in counts.items()) + ' -> judge show', flush=True)
        time.sleep(60)


class JudgeVerb(CommanderVerb):
    """A verb of `judge`."""


@dataclass(frozen=True, kw_only=True)
class ShowVerb(JudgeVerb):
    help = "every unit's unjudged edits in full, beside its current orders"
    thread: str | None = option("thread", nargs="?", default=None)

    def selects(self, edit: Edit) -> bool:
        return self.thread is None or edit.thread == self.thread

    def run(self) -> None:
        show([edit for edit in Edit.unjudged(order.COMMS, ME) if self.selects(edit)])


class AlarmVerb(JudgeVerb):
    help = "loop: every minute, name edits unjudged for 5 min"

    def run(self) -> None:
        alarm()


@dataclass(frozen=True, kw_only=True)
class VerdictVerb(JudgeVerb, RuledVerb):
    """Every verdict, mine or a fork's, is made after reviewing paper 1's
    correct-maintenance results. Each member's required options are the facts its verdict carries."""
    ids: tuple[str, ...] = option("ids", nargs="+", metavar="ID:CODE")
    why: str = option()
    msg: str | None = option(default=None, help="BUSID of my order message to the unit")
    lines: tuple[str, ...] = option(
        "--line", action="append", metavar="LINE", help="an exact diff line; repeat for each")

    @abstractmethod
    def operation(self, todo: Todo, edit_id: str) -> OrderVerdict: ...

    @abstractmethod
    def check_edits(self, edits: list[Edit]) -> None: ...

    @abstractmethod
    def report(self, todos: tuple[Todo, ...]) -> None: ...

    def run(self) -> None:
        if len(self.why.split()) < 6:
            sys.exit('Refused: state which intent the edit serves or violates, and why (6+ words).')
        orders = {todo.id: todo for todo in order.held_orders() if self.msg is None or todo.sent_in(self.msg)}
        edits = {edit.id: edit for edit in Edit.unjudged(order.COMMS, ME) if edit.problem in orders}
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
            self.operation(todo, i).prepare(order.COMMS.goals, todo).apply(todo)
        current = {todo.id: todo for todo in selected.values()}
        for i, todo in selected.items():
            result = order.call(CommsVerdictTool(id=todo.id, operation=self.operation(current[todo.id], i)))
            current[todo.id] = result.todo
        left = len(Edit.unjudged(order.COMMS, ME))
        print(f'{self.declared_name}: {len(ids)} edits; {left} still unjudged')
        self.report(tuple(current.values()))


@dataclass(frozen=True, kw_only=True)
class OkVerb(VerdictVerb):
    help = "the edit serves the order"
    serves: str = option(help="exact words of the order")
    no_owner: str | None = option("--no-owner", default=None, help="12+ words with because and absolute file:line")

    def operation(self, todo: Todo, edit_id: str) -> AcceptedVerdict:
        return AcceptedVerdict(todo.revision, order.COMMS.registry.require(ME).incarnation,
                               todo.held(), edit_id, quote=self.serves)

    def check_edits(self, edits: list[Edit]) -> None:
        for edit in edits:
            path = edit.path
            after = [open(path).read()] if path.endswith('.py') and os.path.exists(path) else []
            if added := order.forwarders_added(edit.added(), after):
                sys.exit(f'Refused: edit {edit.id} adds forwarders {added[:5]}; it goes back with judge back, never ok')
            AcceptedVerdict.require_branches(edit.branches_added(), self.no_owner)

    def report(self, todos: tuple[Todo, ...]) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class SupersededVerb(VerdictVerb):
    help = "the task moved to another unit; these edits are neither ok nor sent back"
    by: str = option("--by", help="the replacing order's message")

    def operation(self, todo: Todo, edit_id: str) -> SupersededVerdict:
        return SupersededVerdict(todo.revision, order.COMMS.registry.require(ME).incarnation,
                                 todo.held(), edit_id, replacement=self.by, reason=self.why)

    def check_edits(self, edits: list[Edit]) -> None:
        pass

    def report(self, todos: tuple[Todo, ...]) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class BackVerb(VerdictVerb):
    help = "send the edit back; the roast is posted to the verdict's audience"
    msg: str = option(help="the required order message")
    roast: str = option(help="blunt, why it is stupid, with because")
    fix: str = option(help="owner and replacing shape")
    slop_kind: str = option("--slop", help="|".join(SlopKind.names()))
    fact: str = option(help="the required fact, 4+ words")
    at: str = option(help="PATH:LINE where it is restated")
    derive: str = option(help="|".join(Derivation.names()))
    by: str = option(help="PATH:LINE of the declaration that owns it")

    def slop(self) -> Slop:
        return FieldCodec.decode(Slop, dict(kind=self.slop_kind, fact=self.fact,
                                           at=self.at, derive=self.derive, by=self.by))

    def operation(self, todo: Todo, edit_id: str) -> SendBackVerdict:
        return SendBackVerdict(todo.revision, order.COMMS.registry.require(ME).incarnation,
                               todo.held(), edit_id,
                               reason=StrikeReason(f'{todo.id} edit sent back: {self.why}'))

    def check_edits(self, edits: list[Edit]) -> None:
        if len(self.roast.split()) < 12 or not re.search(r'\bbecause\b', self.roast):
            sys.exit('Refused: --roast needs 12+ words with because, naming the duplicated fact and its owner')
        if len(self.fix.split()) < 8:
            sys.exit('Refused: --fix needs 8+ words naming the owner and replacing shape')
        self.slop()
        order.refuse(self.roast, BluntText)
        order.refuse(' '.join((self.why, self.roast, self.fix)), *order.OUTGOING)

    def report(self, todos: tuple[Todo, ...]) -> None:
        for todo in todos:
            audience = todo.verdicts[-1].audience.of(todo.held().owner, todo.goal)
            roast(audience, todo.holder, self.lines, self.roast, self.fix, self.slop())
            line = (f'{time.strftime("%Y-%m-%d %H:%M")} {todo.holder} '
                    f'(strike {todo.strike_count} on {todo.id}; caught by {ME} at review): '
                    f'{todo.strikes[-1].reason}')
            order.on_record(line, 'Send-back', audience)


def main(args):
    JudgeVerb.parse(args or ['show'], __doc__).apply()
