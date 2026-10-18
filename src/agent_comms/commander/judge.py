"""The commander's verdict on every unit edit, judged against the order that unit is carrying out.

A send-back's --roast is posted to #comms with the unit and the quoted slop line, so the whole
fleet learns what was stupid and nobody repeats it; a send-back without a roast is refused.
--peer lets --serves quote another agent's message to that unit instead of mine.

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
from dataclasses import dataclass

from ..todos import BluntText
from . import order
from ..cli_commands import option
from .order import CommanderVerb, RuledVerb
from .config import CommanderConfig

CONFIG = CommanderConfig.installed()
ME = CONFIG.commander
RTS = str(CONFIG.state)
EDITS, VERDICTS = f'{RTS}/edits.jsonl', f'{RTS}/verdicts.jsonl'
BUS = f'{CONFIG.root}/bus.jsonl'


def rows(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def unjudged():
    judged = {v['id'] for v in rows(VERDICTS)}
    return [e for e in rows(EDITS) if e['id'] not in judged]


def my_messages(thread):
    """The commander's messages to thread, newest first: one bounded page of the bus's DM history."""
    page = order.COMMS.views.dm_history_page(ME, thread)
    return [dict(id=m.message_id, text=m.body) for m in reversed(page.messages) if m.sender == ME]


BRANCH = re.compile(r'\bisinstance\(|\bhasattr\(|^\s*(if|elif|match|case)\b|\bif\b.*\belse\b')


def branches_added(edit) -> int:
    """Branches the edit's new text has beyond its old text (branch-vs-polymorphism: presumed slop)."""
    count = lambda text: sum(1 for line in text.splitlines() if BRANCH.search(line))
    return sum(count(h['new']) - count(h['old']) for h in edit['hunks'])


def check_code(edit):
    """Four hex digits derived from the edit's own text; shown only after its full hunks."""
    return hashlib.sha256((edit['id'] + json.dumps(edit['hunks'])).encode()).hexdigest()[:4]


def show(thread=None):
    pending = [e for e in unjudged() if thread in (None, e['thread'])]
    by_thread = {}
    for e in pending:
        by_thread.setdefault(e['thread'], []).append(e)
    for name, edits in by_thread.items():
        orders = my_messages(name)
        intent = (orders[0].get('text') or '')[:1500] if orders else 'NO ORDER FROM ME: what is this unit doing and who asked?'
        print(f'===== {name}: {len(edits)} unjudged\nCURRENT ORDER ({orders[0]["id"] if orders else "-"}):\n{intent}\n')
        for e in edits:
            print(f'--- {e["id"]} {time.strftime("%H:%M", time.localtime(e["at"]))} {e["path"]}')
            for h in e['hunks']:
                print(''.join(f'- {l}\n' for l in h['old'].splitlines()) + ''.join(f'+ {l}\n' for l in h['new'].splitlines()))
            print(f'=== end {e["id"]} code {check_code(e)}')
    print(f'{len(pending)} unjudged')


def peer_order(peer, thread):
    """Text of another agent's recent message PEER to thread (work a peer legitimately assigned)."""
    if peer is None:
        return ''
    with open(BUS, 'rb') as f:
        f.seek(max(0, os.path.getsize(BUS) - 200_000_000))
        f.readline()
        for line in f:
            if peer.encode() in line[:300]:
                m = json.loads(line)
                to = m['to'] if isinstance(m['to'], list) else [m['to']]
                if m['id'] == peer and thread in to:
                    return m.get('text') or ''
    sys.exit(f'Refused: {peer} is not a recent message to {thread}')


def roast(thread, lines, text, fix, slop):
    """Every send-back is a fleet lesson, sent to the unit that wrote the slop and to #comms: the quoted slop,
    the required fact it restates and the declaration that derives it (paper 1's definition), why that is
    stupid, and what replaces it."""
    body = (f'{thread}, this is stupid slop and it is going back:\n'
            + '\n'.join(f'    {l.strip()}' for l in lines)
            + f'\nThe slop: {slop.render()}'
            + f'\nWhy it is stupid: {text}\nWhat replaces it: {fix}\nNobody writes this again.')
    print('roast sent to the unit and #comms: ' + order.post([thread, '#comms'], body))


def strike_task(thread, msg):
    """The task a send-back strikes: the P-number in the cited message, else the one task of the unit's open order.

    Decided before the verdict is written, so a send-back is never recorded without its strike.
    """
    if found := re.search(r'\bP\d+\b', next(m['text'] for m in my_messages(thread) if m['id'] == msg)):
        return found.group()
    open_facts = sorted(t.id for t in order.COMMS.goals.todos.held_for_commander(
        order.COMMS.registry.require(order.ME).incarnation, thread))
    if len(open_facts) != 1:
        sys.exit(f'Refused: {msg} names no P-number and {thread} has open tasks {open_facts}; cite the order message for the task')
    return open_facts[0]


def verdict(kind, tokens, why, msg=None, serves=None, lines=(), peer=None, roast_text=None, fix=None, slop=None, no_owner=None):
    if len(why.split()) < 6:
        sys.exit('Refused: state which intent the edit serves or violates, and why (6+ words).')
    edits = {e['id']: e for e in rows(EDITS)}
    ids = [t.split(':')[0] for t in tokens]
    missing = [i for i in ids if i not in edits]
    if missing:
        sys.exit(f'Refused: unknown edit ids {missing}')
    unread = [t for t in tokens if t != f'{t.split(":")[0]}:{check_code(edits[t.split(":")[0]])}']
    if unread:
        sys.exit(f'Refused: wrong or missing read codes {unread}; read each edit to its "=== end" line')
    if not lines:
        sys.exit('Refused: quote, with --line, the exact diff line each edit is judged by')
    for i in ids:
        text = ''.join(h['old'] + h['new'] for h in edits[i]['hunks'])
        if not any(l in text for l in lines):
            sys.exit(f'Refused: none of your --line quotes is in edit {i}; judge it by its own text')
    if kind == 'ok':
        for i in ids:
            diff = ''.join(''.join('+' + l + '\n' for l in h['new'].splitlines()) for h in edits[i]['hunks'])
            path = edits[i].get('path') or ''
            after = [open(path).read()] if path.endswith('.py') and os.path.exists(path) else []
            if added := order.forwarders_added(diff, after):
                sys.exit(f'Refused: edit {i} adds forwarders {added[:5]}; it goes back with judge back, never ok')
            if (extra := branches_added(edits[i])) > 0 and not (
                    no_owner and len(no_owner.split()) >= 12 and re.search(r'\bbecause\b', no_owner)
                    and re.search(r'\S+\.py:\d+', no_owner)):
                sys.exit(f'Refused: edit {i} adds {extra} branch(es); branching is presumed slop. Send it back naming the '
                         'base or ABC method that owns the decision, or pass --no-owner "<12+ words with because and '
                         'file:line>" proving no type could own it')
        for thread in {edits[i]['thread'] for i in ids}:
            orders = ' '.join(m['text'] for m in my_messages(thread)) + ' ' + peer_order(peer, thread)
            if not serves or len(serves.split()) < 4 or ' '.join(serves.split()) not in ' '.join(orders.split()):
                sys.exit(f'Refused: --serves must quote 4+ words of an order I sent {thread}; an edit no order asked for is not ok')
    if kind == 'back':
        if not roast_text or len(roast_text.split()) < 12 or not re.search(r'\bbecause\b', roast_text):
            sys.exit('Refused: --roast (12+ words, with "because"): why this is stupid, naming the fact it restates or guesses and the thing that already answers it')
        if not fix or len(fix.split()) < 8:
            sys.exit('Refused: --fix (8+ words): the owner and the exact shape that replaces the slop')  # the strike's own wording rule, checked before the verdict is written
        order.refuse(roast_text, BluntText)
        order.refuse(' '.join((why, roast_text, fix)), *order.OUTGOING)  # --line quotes the unit
        tasks = {}
        for thread in {edits[i]['thread'] for i in ids}:
            if msg not in {m['id'] for m in my_messages(thread)}:
                sys.exit(f'Refused: {msg} is not a message from {ME} to {thread}')
            tasks[thread] = strike_task(thread, msg)
    with open(VERDICTS, 'a') as f:
        for i in ids:
            f.write(json.dumps(dict(id=i, kind=kind, why=why, msg=msg, serves=serves, lines=lines, no_owner=no_owner, at=time.time())) + '\n')
    print(f'{kind}: {len(ids)} edits; {len(unjudged())} still unjudged')
    if kind == 'back':
        for thread in sorted({edits[i]['thread'] for i in ids}):
            roast(thread, lines, roast_text, fix, slop)
            # A send-back is a strike; recorded here so it cannot be skipped.
            order.strike(thread, f'{tasks[thread]} edit sent back: {why}')


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
class OkVerb(JudgeVerb, RuledVerb):
    """Every verdict, mine or a fork's, is made after reviewing paper 1's
    correct-maintenance results."""
    help = "the edit serves the order"
    ids: tuple[str, ...] = option("ids", nargs="+", metavar="ID:CODE")
    why: str = option("--why", default="")
    msg: str | None = option("--msg", default=None, help="BUSID of my message to the unit")
    serves: str | None = option("--serves", default=None, help="exact words of the order")
    lines: tuple[str, ...] = option(
        "--line", default=(), parser_default_factory=list, action="append",
        metavar="LINE", help="an exact diff line; repeat for each")
    peer: str | None = option(
        "--peer", default=None, help="BUSID of another agent's message that --serves quotes")
    roast: str | None = option(
        "--roast", default=None, help="blunt, why it is stupid, with because")
    fix: str | None = option("--fix", default=None, help="owner and replacing shape")
    no_owner: str | None = option("--no-owner", default=None)

    def slop(self) -> order.Slop | None:
        return None

    def run(self) -> None:
        verdict(self.declared_name, list(self.ids), self.why, self.msg, self.serves,
                list(self.lines), self.peer, self.roast, self.fix, self.slop(), self.no_owner)


@dataclass(frozen=True, kw_only=True)
class BackVerb(OkVerb):
    help = "send the edit back; the roast is posted to #comms"
    slop_kind: str = option("--slop", help="|".join(order.Slop.KINDS))
    fact: str = option("--fact", help="the required fact, 4+ words")
    at: str = option("--at", help="PATH:LINE where it is restated")
    derive: str = option("--derive", help="|".join(order.Slop.MECHANISMS))
    by: str = option("--by", help="PATH:LINE of the declaration that owns it")

    def slop(self) -> order.Slop:
        return order.Slop(self.slop_kind, self.fact, self.at, self.derive, self.by)


def main(args):
    JudgeVerb.parse(args or ['show'], __doc__).apply()
