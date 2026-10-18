"""The commander's verdict on every unit edit, judged against the order that unit is carrying out.

A send-back's --roast is posted to #comms with the unit and the quoted slop line, so the whole
fleet learns what was stupid and nobody repeats it; a send-back without a roast is refused.
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
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from ..todos import AcceptedVerdict, BluntText, OrderVerdict, SendBackVerdict, StrikeReason, Todo
from ..tools import CommsVerdictTool
from . import order
from ..cli_commands import option
from .order import CommanderVerb, RuledVerb
from .config import CommanderConfig

CONFIG = CommanderConfig.installed()
ME = CONFIG.commander
RTS = str(CONFIG.state)
EDITS = f'{RTS}/edits.jsonl'


class Hunk(TypedDict):
    old: str
    new: str


class Edit(TypedDict):
    """One row of edits.jsonl: a unit's file edit awaiting its verdict."""
    id: str
    thread: str
    at: float
    path: NotRequired[str]
    hunks: list[Hunk]


def rows(path: str) -> list[Edit]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def unjudged() -> list[Edit]:
    judged = {v.tool_call_id for todo in order.COMMS.goals.todos.history_for_commander(
        order.COMMS.registry.require(ME).incarnation) for v in todo.verdicts}
    return [e for e in rows(EDITS) if e['id'] not in judged]


def my_messages(thread: str) -> list[dict[str, str]]:
    """The commander's messages to thread, newest first: one bounded page of the bus's DM history."""
    page = order.COMMS.views.dm_history_page(ME, thread)
    return [dict(id=m.message_id, text=m.body) for m in reversed(page.messages) if m.sender == ME]


BRANCH = re.compile(r'\bisinstance\(|\bhasattr\(|^\s*(if|elif|match|case)\b|\bif\b.*\belse\b')


def branches_added(edit: Edit) -> int:
    """Branches the edit's new text has beyond its old text (branch-vs-polymorphism: presumed slop)."""
    count = lambda text: sum(1 for line in text.splitlines() if BRANCH.search(line))
    return sum(count(h['new']) - count(h['old']) for h in edit['hunks'])


def check_code(edit: Edit) -> str:
    """Four hex digits derived from the edit's own text; shown only after its full hunks."""
    return hashlib.sha256((edit['id'] + json.dumps(edit['hunks'])).encode()).hexdigest()[:4]


def show(thread=None):
    pending = [e for e in unjudged() if thread in (None, e['thread'])]
    by_thread = {}
    for e in pending:
        by_thread.setdefault(e['thread'], []).append(e)
    for name, edits in by_thread.items():
        orders = order.COMMS.goals.todos.held_for_commander(order.COMMS.registry.require(ME).incarnation, name)
        intent = '\n'.join(todo.order.instructions(todo) for todo in orders) or 'NO OPEN ORDER TODO'
        print(f'===== {name}: {len(edits)} unjudged\nCURRENT ORDERS:\n{intent}\n')
        for e in edits:
            print(f'--- {e["id"]} {time.strftime("%H:%M", time.localtime(e["at"]))} {e["path"]}')
            for h in e['hunks']:
                print(''.join(f'- {l}\n' for l in h['old'].splitlines()) + ''.join(f'+ {l}\n' for l in h['new'].splitlines()))
            print(f'=== end {e["id"]} code {check_code(e)}')
    print(f'{len(pending)} unjudged')


def roast(thread, lines, text, fix, slop):
    """Every send-back is a fleet lesson, sent to the unit that wrote the slop and to #comms: the quoted slop,
    the required fact it restates and the declaration that derives it (paper 1's definition), why that is
    stupid, and what replaces it."""
    body = (f'{thread}, this is stupid slop and it is going back:\n'
            + '\n'.join(f'    {l.strip()}' for l in lines)
            + f'\nThe slop: {slop.render()}'
            + f'\nWhy it is stupid: {text}\nWhat replaces it: {fix}\nNobody writes this again.')
    print('roast sent to the unit and #comms: ' + order.post([thread, '#comms'], body))


def alarm():
    while True:
        late = [e for e in unjudged() if time.time() - e['at'] > 300]
        if late:
            counts = {}
            for e in late:
                counts[e['thread']] = counts.get(e['thread'], 0) + 1
            oldest = min(e['at'] for e in late)
            print(f'UNJUDGED {len(late)} edits over 5 min (oldest {int((time.time() - oldest) / 60)} min): '
                  + ', '.join(f'{t} {n}' for t, n in counts.items()) + ' -> judge show', flush=True)
        time.sleep(60)


class JudgeVerb(CommanderVerb):
    """A verb of `judge`."""


@dataclass(frozen=True, kw_only=True)
class ShowVerb(JudgeVerb):
    help = "unjudged edits in full, beside the unit's current order"
    thread: str | None = option("thread", nargs="?", default=None)

    def run(self) -> None:
        show(self.thread)


class AlarmVerb(JudgeVerb):
    help = "loop: every minute, name edits unjudged for 5 min"

    def run(self) -> None:
        alarm()


@dataclass(frozen=True, kw_only=True)
class VerdictVerb(JudgeVerb, RuledVerb):
    """Every verdict, mine or a fork's, is made after reviewing paper 1's
    correct-maintenance results. Each member declares the facts its verdict carries as required options."""
    ids: tuple[str, ...] = option("ids", nargs="+", metavar="ID:CODE")
    why: str = option("--why")
    msg: str = option("--msg", help="BUSID of my order message to the unit")
    lines: tuple[str, ...] = option(
        "--line", action="append", metavar="LINE", help="an exact diff line; repeat for each")

    @abstractmethod
    def operation(self, todo: Todo, edit_id: str) -> OrderVerdict: ...

    @abstractmethod
    def check_edits(self, edits: list[Edit]) -> None: ...

    @abstractmethod
    def report(self, todos: tuple[Todo, ...]) -> None: ...

    def held_order(self, edit: Edit) -> Todo:
        """Resolve an edit to the order my cited message names; never record a verdict without that Todo."""
        cited = next((m for m in my_messages(edit['thread']) if m['id'] == self.msg), None)
        if cited is None:
            sys.exit(f'Refused: edit {edit["id"]}: {self.msg} is not a message from {ME} to {edit["thread"]}')
        candidates: list[Todo] = order.COMMS.goals.todos.held_for_commander(
            order.COMMS.registry.require(ME).incarnation, edit['thread'])
        if found := re.search(r'\bP\d+\b', cited['text']):
            candidates = [todo for todo in candidates if todo.id == found.group()]
        if len(candidates) != 1:
            sys.exit(f'Refused: edit {edit["id"]} by {edit["thread"]} needs one open order Todo; '
                     f'found {[todo.id for todo in candidates]}; cite the message that names its P-number')
        return candidates[0]

    def run(self) -> None:
        if len(self.why.split()) < 6:
            sys.exit('Refused: state which intent the edit serves or violates, and why (6+ words).')
        edits = {e['id']: e for e in rows(EDITS)}
        ids = [token.split(':')[0] for token in self.ids]
        if missing := [i for i in ids if i not in edits]:
            sys.exit(f'Refused: unknown edit ids {missing}')
        if unread := [token for token in self.ids
                      if token != f'{token.split(":")[0]}:{check_code(edits[token.split(":")[0]])}']:
            sys.exit(f'Refused: wrong or missing read codes {unread}; read each edit to its "=== end" line')
        for i in ids:
            text = ''.join(h['old'] + h['new'] for h in edits[i]['hunks'])
            if not any(line in text for line in self.lines):
                sys.exit(f'Refused: none of your --line quotes is in edit {i}; judge it by its own text')
        selected = {i: self.held_order(edits[i]) for i in ids}
        self.check_edits([edits[i] for i in ids])
        # Validate the whole batch through the operation owner before its first write.
        for i, todo in selected.items():
            self.operation(todo, i).apply(todo)
        current = {todo.id: todo for todo in selected.values()}
        for i, todo in selected.items():
            result = order.call(CommsVerdictTool(id=todo.id, operation=self.operation(current[todo.id], i)))
            current[todo.id] = result.todo
        print(f'{self.declared_name}: {len(ids)} edits; {len(unjudged())} still unjudged')
        self.report(tuple(current.values()))


@dataclass(frozen=True, kw_only=True)
class OkVerb(VerdictVerb):
    help = "the edit serves the order"
    serves: str = option("--serves", help="exact words of the order")

    def operation(self, todo: Todo, edit_id: str) -> AcceptedVerdict:
        return AcceptedVerdict(todo.revision, order.COMMS.registry.require(ME).incarnation,
                               todo.held(), edit_id, quote=self.serves)

    def check_edits(self, edits: list[Edit]) -> None:
        for edit in edits:
            diff = ''.join('+' + line + '\n' for h in edit['hunks'] for line in h['new'].splitlines())
            path = edit.get('path') or ''
            after = [open(path).read()] if path.endswith('.py') and os.path.exists(path) else []
            if added := order.forwarders_added(diff, after):
                sys.exit(f'Refused: edit {edit["id"]} adds forwarders {added[:5]}; it goes back with judge back, never ok')
            if (extra := branches_added(edit)) > 0:
                self.own_branches(edit, extra)

    def own_branches(self, edit: Edit, extra: int) -> None:
        sys.exit(f'Refused: edit {edit["id"]} adds {extra} branch(es); send it back, or judge it with ok-unowned '
                 'and --no-owner proving no type could own them')

    def report(self, todos: tuple[Todo, ...]) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class OkUnownedVerb(OkVerb, declared_name="ok-unowned"):
    help = "the edit serves the order and its added branches have no type that could own them"
    no_owner: str = option("--no-owner", help="12+ words with because and file:line")

    def own_branches(self, edit: Edit, extra: int) -> None:
        if not (len(self.no_owner.split()) >= 12 and re.search(r'\bbecause\b', self.no_owner)
                and re.search(r'\S+\.py:\d+', self.no_owner)):
            sys.exit(f'Refused: edit {edit["id"]} adds {extra} branch(es); --no-owner needs 12+ words, '
                     'because and file:line proving no type could own them')


@dataclass(frozen=True, kw_only=True)
class BackVerb(VerdictVerb):
    help = "send the edit back; the roast is posted to #comms"
    roast: str = option("--roast", help="blunt, why it is stupid, with because")
    fix: str = option("--fix", help="owner and replacing shape")
    slop_kind: str = option("--slop", help="|".join(order.Slop.KINDS))
    fact: str = option("--fact", help="the required fact, 4+ words")
    at: str = option("--at", help="PATH:LINE where it is restated")
    derive: str = option("--derive", help="|".join(order.Slop.MECHANISMS))
    by: str = option("--by", help="PATH:LINE of the declaration that owns it")

    def slop(self) -> order.Slop:
        return order.Slop(self.slop_kind, self.fact, self.at, self.derive, self.by)

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
            roast(todo.holder, self.lines, self.roast, self.fix, self.slop())
            line = (f'{time.strftime("%Y-%m-%d %H:%M")} {todo.holder} '
                    f'(strike {todo.strike_count} on {todo.id}; caught by {ME} at review): '
                    f'{todo.strikes[-1].reason}')
            order.on_record(line, 'Send-back')


def main(args):
    JudgeVerb.parse(args or ['show'], __doc__).apply()
