"""The only way the commander sends an order to a unit. Refuses soft or vague orders; tracks deadlines and strikes.

  order send UNIT DUE(HH:MM) BODYFILE [--clean] [--reassign "<concrete failure>"] [--replaces MSG]   send a numbered order with a deadline
     the body must declare, on their own lines:
       Problems: P<n>[, P<m>...]                       (the listed problems it owns, batched by surface for one owner; one open order per problem)
       Owner: <path>:<line>                              (the existing owner the fix goes on; must exist)
       Check: <repo> :: <shell command> -> <integer>    (done runs it; the last integer printed must match)
                                             (--clean only when the unit showed no incompetence)
     --replaces without --clean is a send-back: it strikes the unit and roasts the blunt line in #comms
  order say UNIT[,UNIT] BODYFILE           a non-order message (answer, ownership call); refused if it orders work
  order problem "TEXT" --where PATH:LINE     record a problem (it alarms every minute until an order owns it)
  order close PID --works "EVIDENCE" --artifact FILE   the fix works in the app; Core closes the problem
  order fold PID --into PID --why "..."     a duplicate row joins the problem that already states it
  order board                               regenerate fleet-board.md from problems and orders
  order done UNIT MSG --sha SHA           the unit delivered that order (SHA it pushed); late delivery is a strike
  order drop UNIT MSG --by NEWMSG           an older order replaced by a newer order to the same unit (no credit, no strike)
  order strike UNIT --why "..." [--caught "WHO at STAGE"]   a send-back, a missed deadline or a false claim; stage is review/install/live/user
  order overdue                           loop: every minute, name units past their deadline

Incompetence is not tolerated: a unit is fired from a task, not berated about it.
Two strikes on one task: the task is taken away and reassigned (`fire`). Nothing here judges
code; it refuses orders that leave a unit room to interpret, and refuses to let a deadline lapse silently.
"""
import calendar, fcntl, json, os, re, subprocess, sys, time

from ..comms import Comms
from ..tool_caller import ThreadCaller
from ..tools import CommsProblemCloseTool
from . import storage
from .config import CommanderConfig

CONFIG = CommanderConfig.installed()
ME = CONFIG.commander
COMMS = Comms(CONFIG.root)  # the bus whose Todo store owns problems, orders, deadlines and strikes
RTS = str(CONFIG.state)
DEADLINES, STRIKES = f'{RTS}/deadlines.json', f'{RTS}/strikes.jsonl'
DONE = f'{RTS}/done.jsonl'  # every delivered order, written by done(); the board shows its problem as install-pending
SENT = f'{RTS}/sent.jsonl'  # every message the commander sends to units, written where it is sent
PROBLEMS = f'{RTS}/problems.json'  # the one problem list; fleet-board.md is generated from it
BOARD = str(CONFIG.board)
NARRATIVE = str(CONFIG.narrative)  # public record of every failure, in plain words
# Words that hide what happened. A strike must say what the unit did in words the user uses.
JARGON = re.compile(r'\b(custody|admission|admit\w*|receipt\w*|checkpoint\w*|provenance|authority|authorities|witness\w*|lattice|surface|disposition\w*|invariant\w*|semantic\w*|ownership|lifecycle|acceptance|canonical|retained|borrow(?:ed|er|ers)?|bounded|certif\w*|recertif\w*|candidate|fence\w*|sidecar\w*|substrate|installed-path|source[- ]paired|exact[- ]head|integration owner|separately owned|follow-?up|remaining scope|not claimed|remains held|load[- ]bearing|cutover|carr(?:y|ies) across|declared dependency|qualifying)\b', re.I)


def jargon_in(text: str) -> list[str]:
    """Jargon in plain prose: quoted text, `code`, indented quote lines and paths are someone else's words, not the sentence."""
    prose = re.sub(r'(?m)^ {4}.*$|`[^`\n]*`|"[^"\n]*"|\S*/\S*|\S+\.(?:py|md|jsonl?|txt|png|toml|ts|log|patch)\b', ' ', text)
    return sorted({m.group(0).lower() for m in JARGON.finditer(prose)})
B = str(CONFIG.root)
CLI = str(CONFIG.cli)

# Phrases that hand the decision back to the unit or ask permission instead of ordering.
SOFT = re.compile(r"\b(please|could you|would you|can you|if possible|consider|maybe|perhaps|when you get a chance|"
                  r"feel free|if you (think|agree|want)|thoughts\??|what do you think|thanks|thank you|sorry|"
                  r"no rush|optional(ly)?|up to you|might want|suggest)\b", re.I)


def post(recipients, body):
    """Send one message from the commander and record it in SENT; judge reads orders from there."""
    if found := jargon_in(body):
        sys.exit(f'REFUSED, nothing sent: your own message uses jargon {found}. Say what happened in plain words.')
    refuse_dogma(re.sub(r'(?m)`[^`\n]*`|"[^"\n]*"|^ {4}.*$', ' ', body))  # quoting the dogma to correct it is allowed
    command = ['timeout', '300', CLI, '--root', B, 'send', '--from', ME]
    for unit in recipients:
        command += ['--to', unit]
    out = subprocess.run(command + ['--type', 'info', '--body', body], capture_output=True, text=True)
    if '"id"' not in out.stdout:
        sys.exit(f'Send failed: {out.stdout}{out.stderr}')
    sent = json.loads(out.stdout)['id']
    with open(SENT, 'a') as f:
        f.write(json.dumps(dict(id=sent, to=list(recipients), text=body, at=time.time())) + '\n')
    return sent


def facts(order):
    """The problems one order owns: a batch of related problems goes to one owner as one order."""
    return order.get('facts') or ([order['fact']] if order.get('fact') else [])


_held = None  # deadlines.json lock: held from the first read until this command writes or exits


def load(path, empty):
    """Reading deadlines.json takes its lock, so no other order run writes back a stale copy over this one's."""
    global _held
    if path == DEADLINES and _held is None:
        _held = open(DEADLINES + '.lock', 'w')
        fcntl.flock(_held, fcntl.LOCK_EX)
    return json.load(open(path)) if os.path.exists(path) else empty


def save(path, value):
    """Atomic replace, so unlocked readers (rotate, judge) never see half a file; releases the deadlines lock."""
    global _held
    temporary = f'{path}.{os.getpid()}.tmp'
    with open(temporary, 'w') as f:
        json.dump(value, f, indent=1)
    os.replace(temporary, path)
    if path == DEADLINES:
        release()


def release():
    global _held
    if _held is not None:
        fcntl.flock(_held, fcntl.LOCK_UN)
        _held.close()
        _held = None


# Paper 1 proves multiple
# parents derive every required pair with nothing outside (the top rung), that composition and forwarding supply
# the same pairs outside (Fig 3, Thm 4.6), and that is-a and has-a are not opposites. A reason that faults
# inheritance, mixins or multiple parents as such is dogma, not a restated fact, and is refused wherever the
# commander writes it. A real defect is stated as a Slop: line instead.
DOGMA = re.compile(r"composition over inheritance|(prefer|favou?r|use) composition|inherits (two|both|multiple|several|\d+)\b"
                   r"|multiple (inheritance|parents) (is (bad|wrong|dangerous|confusing|fragile|slop)|causes|creates|smells|complicates)|diamond problem|fragile base"
                   r"|(avoid|no|stop using|drop) (inheritance|mixins?|multiple (parents|inheritance))|too many (parents|base classes|mixins)"
                   r"|deep (inheritance|hierarch)|mixing (of )?\w+ and \w+|inheritance (abuse|soup|hell|is wrong)", re.I)


def refuse_dogma(text):
    if found := DOGMA.search(re.sub(r'(?m)^Banned:.*$', '', text)):
        sys.exit(f'REFUSED, nothing recorded: "{found.group()}" faults inheritance itself. Paper 1 proves multiple parents derive '
                 'what composition and forwarding supply outside (Thm 4.6, Fig 3). Name the restated fact in a Slop: line '
                 '(identity, membership, implementation or state) and the declaration that derives it.')


# Eliminate forwarding with inheritance and multiple parents; decompose the hierarchy if it is not sound.
# A forwarder restates an implementation pair (paper 1 Fig 3: fields + forwarding leave routes to maintain), so
# nothing the commander approves, delivers or orders may add or keep one. The cure is the hierarchy, never a
# route: fold the false owner into the real one, or compose the providers as parents.
FORWARDER = re.compile(
    r"^\+\s*(?:def \w+\([^)]*\)(?:\s*->[^:]+)?:\s*return self\.\w+\.\w+(?:\([^()]*\))?\s*$"      # one-line def returning self.x.y(...)
    r"|\w+\s*=\s*property\(lambda \w+:\s*\w+\.\w+\.\w+\)"                                        # attr = property(lambda s: s._a.attr)
    r"|def __getattr__\(self)", re.M)
FORWARD_BODY = re.compile(r"^\+(\s*)def (\w+)\(self[^)]*\)[^:]*:\s*\n\+\1\s+return self\.\w+\.\2\b", re.M)  # def x(self): / return self._a.x(...)
KEEP_SLOP = re.compile(r"(leave|keep|retain|preserve)[^.\n]{0,60}\bforward|forwarders? (stay|remain)|reach(es|ing)? through"
                       , re.I | re.M)
HIERARCHY_WORD = re.compile(r"\b(new class(es)?|new base|base class|changing the hierarchy|inheritance|mixins?|multiple parents|changing \w+State)\b", re.I)
UNDOES_HIERARCHY = re.compile(r"\b(flatten\w*|remov\w*|replac\w*|delet\w*|collaps\w*|single-parent|one-parent|merg\w*)\b", re.I)


def bans_hierarchy_fix(text):
    """A Banned item that forbids adding classes, bases or parents; banning their removal is fine."""
    for line in re.findall(r'(?m)^Banned:(.*)$', text):
        for item in re.split(r',|;|\bor\b', line):
            if HIERARCHY_WORD.search(item) and not UNDOES_HIERARCHY.search(item):
                return item.strip()
    return None


def overrides(sources):
    """Names a class answers again for a question a base in the same module declares: a per-state answer, not forwarding."""
    import ast
    found = set()
    for text in sources:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        classes = {c.name: c for c in tree.body if isinstance(c, ast.ClassDef)}
        def own(c):
            names = set()
            for node in c.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(node.name)
                elif isinstance(node, ast.Assign):
                    names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names.add(node.target.id)
            return names
        def inherited(c, seen=()):
            names = set()
            for b in c.bases:
                if isinstance(b, ast.Name) and b.id in classes and b.id not in seen:
                    names |= own(classes[b.id]) | inherited(classes[b.id], seen + (b.id,))
            return names
        for c in classes.values():
            found |= own(c) & inherited(c)
    return found


def forwarders_added(diff, sources=()):
    """Lines a diff adds that only hand a call to another object. A subclass answering a question its base declares
    (sources: the files after the change) is a per-state override, which is the polymorphism the paper asks for.
    A call that passes the holder itself (`return self.state.ready(self)`) asks the current state object to decide
    for this owner: that is the state pattern doing the deciding, not a hand-off."""
    kept = overrides(sources)
    state_call = re.compile(r'return self\.\w+\.\w+\((?:[^()]*,\s*)?self\b')
    defs = lambda text: re.search(r'def (\w+)', text)
    return ([m.group(0).strip() for m in FORWARDER.finditer(diff)
             if not (defs(m.group(0)) and defs(m.group(0)).group(1) in kept) and not state_call.search(m.group(0))]
            + [f'def {m.group(2)} -> self.?.{m.group(2)}' for m in FORWARD_BODY.finditer(diff)
               if m.group(2) not in kept and not state_call.search(diff[m.start():m.end() + 200].split('\n+', 2)[1] if '\n+' in diff[m.start():m.end()+200] else '')])


def refuse_slop_love(text):
    text = re.sub(r'(?m)`[^`\n]*`|"[^"\n]*"|^ {4}.*$', ' ', text)  # quoting an old order to correct it is allowed
    if found := (m.group() if (m := KEEP_SLOP.search(text)) else bans_hierarchy_fix(text)):
        sys.exit(f'REFUSED, nothing recorded: "{found.strip()}" keeps a forwarder or bans fixing the hierarchy. '
                 'Fold the false owner into the real one or compose providers as parents, and delete every route.')


class Slop:
    """Paper 1's definition, required wherever the commander calls something slop: code restating a required fact
    (an identity, a contract membership, an implementation pair, or a second writable copy of one state) that a
    complete mechanism derives once from its declaration. Naming slop means naming the fact, where it is
    restated, the declaration that should own it and the mechanism that derives it from there; a label alone
    tells the unit nothing it can check."""
    KINDS = {'identity': 'which meaning this is', 'membership': 'which contract or set it belongs to',
             'implementation': 'which implementation it gets', 'state': 'a second writable copy of one value'}
    MECHANISMS = ('nominal-key', 'subclass', 'mixin', 'base-method', 'init-subclass', 'metaclass', 'declaration', 'delete-copy')
    USAGE = ('Refused: slop needs its formal statement: --slop KIND (' + '|'.join(KINDS) + ') --fact "<the required fact, 4+ words>" '
             '--at PATH:LINE (where it is restated) --derive MECH (' + '|'.join(MECHANISMS) + ') --by PATH:LINE (the declaration that owns it)')
    LINE = re.compile(r'^Slop: (\w+); fact (.+?); at (\S+:\d+); derive ([\w-]+) on (\S+:\d+)\s*$', re.M)

    def __init__(self, kind, fact, at, derive, by):
        problems = []
        if kind not in self.KINDS:
            problems.append(f'kind {kind!r} is not one of {sorted(self.KINDS)}')
        if len(fact.split()) < 4:
            problems.append('the fact needs 4+ words')
        if derive not in self.MECHANISMS:
            problems.append(f'mechanism {derive!r} is not one of {self.MECHANISMS}')
        for place in (at, by):
            path, _, line = place.rpartition(':')
            if not (os.path.isfile(path) and line.isdigit() and int(line) <= sum(1 for _ in open(path))):
                problems.append(f'{place} does not exist')
        if problems:
            sys.exit(self.USAGE + '\n  ' + '\n  '.join(problems))
        self.kind, self.fact, self.at, self.derive, self.by = kind, fact, at, derive, by

    @classmethod
    def from_args(cls, args):
        try:
            return cls(*(args[args.index(flag) + 1] for flag in ('--slop', '--fact', '--at', '--derive', '--by')))
        except ValueError:
            sys.exit(cls.USAGE)

    @classmethod
    def from_order(cls, body):
        """Every Slop: line of an order; an order that calls something slop must carry at least one."""
        found = [cls(*m.groups()) for m in cls.LINE.finditer(body)]
        if re.search(r'\bslop\b', re.sub(r'^Slop:.*$', '', body, flags=re.M), re.I) and not found:
            sys.exit('Refused order: it calls something slop without a line\n  Slop: <' + '|'.join(cls.KINDS)
                     + '>; fact <required fact>; at <path:line restated>; derive <' + '|'.join(cls.MECHANISMS) + '> on <path:line owner>')
        return found

    def render(self):
        return (f'{self.at} restates a {self.kind} fact ({self.KINDS[self.kind]}): {self.fact}. '
                f'{self.by} owns it, and {self.derive} derives it from there, so the copy is deleted.')


BLUNT = re.compile(r"\b(stupid|slop|bullshit|useless|wrong|lazy|garbage|nonsense|incompeten\w*|sloppy|theater|dumb|worthless|broke|broken|pointless|waste)\b", re.I)


def send(unit, due, body_file, clean=False, mechanical=False, reassign=None, replaces=None, disk=False):
    body = open(body_file).read()
    problems = []
    # Storage is a hard rule: below the floor no order may ask for work that allocates disk; tool-watch blocks the
    # allocating calls themselves, so source edits on existing checkouts still go out. --disk passes a cleanup order.
    if storage.free_gb() < storage.FLOOR_GB and not disk and (asked := storage.ALLOCATES.search(body)):
        problems.append(f'{CONFIG.disk} has {storage.free_gb():.1f} GB free, under the {storage.FLOOR_GB} GB floor, and the order asks for "{asked.group()}"')
    if quota := storage.over_quota(unit):
        problems.append(f'{quota}: it cleans up its own checkouts and scratch before it gets new work')
    refuse_dogma(body)
    refuse_slop_love(body)
    Slop.from_order(body)  # calling something slop requires the fact, where it is restated, its owner and the mechanism
    # Blunt at all times; the only exception is no witnessed incompetence.
    if not clean and not BLUNT.search(body):
        problems.append('not blunt: name what was stupid/slop/wrong and why, or pass --clean if nothing was')
    # Quoted text is a citation (e.g. naming the banned words), not my own wording.
    unquoted = re.sub(r'"[^"\n]*"|“[^”\n]*”', '', body)
    if soft := sorted({m.group(0).lower() for m in SOFT.finditer(unquoted)}):
        problems.append(f'soft wording {soft}: state the edit, do not ask')
    if len(re.findall(r'(?m)^\s*\d+[.)] ', body)) < 1:
        problems.append('no numbered edits: list each exact edit as "1. ...", "2. ..."')
    if not re.search(r'(?i)\breport\b', body):
        problems.append('no report line: say exactly what the unit sends back (sha, counts before -> after)')
    if not re.search(r'(?i)\bbanned\b|\bno (new|cache|debounce|flag|fallback|local import)', body):
        problems.append('no banned shapes: name the slop shapes this task invites')
    if not re.fullmatch(r'\d{1,2}:\d{2}', due):
        problems.append('due must be HH:MM local time')
    fact = re.search(r'(?m)^Problems?:\s*(P\d+(?:\s*,\s*P\d+)*)\b', body)
    group = re.findall(r'P\d+', fact[1]) if fact else []
    owner = re.search(r'(?m)^Owner:\s*(\S+):(\d+)\s*$', body)
    check = re.search(r'(?m)^Check:\s*(\S+)\s*::\s*(.+?)\s*->\s*(\d+)\s*$', body)
    if not fact:
        problems.append('no Problem: P<n> line; record it first with `order problem` (every order owns a listed problem)')
    elif any(pid not in load(PROBLEMS, {}) for pid in group):
        problems.append(f'{[pid for pid in group if pid not in load(PROBLEMS, {})]} not open problems')
    if not owner:
        problems.append('no Owner: path:line of the existing owner the fix belongs on')
    elif not os.path.exists(owner[1]) or int(owner[2]) > sum(1 for _ in open(owner[1])):
        problems.append(f'Owner {owner[1]}:{owner[2]} does not exist; read the code and name the real owner')
    if not check:
        problems.append('no Check: <repo> :: <command> -> N; done runs it, so make it the real acceptance count')
    elif not os.path.isdir(check[1]):
        problems.append(f'Check repo {check[1]} is not a directory')
    # A check that can never print its target, or a premise naming lines that do something else, wastes the order.
    # An order's check must fail now (or it proves nothing), and every absolute
    # path:line it cites must exist.
    if check and os.path.isdir(check[1]) and not problems:
        try:
            ran = subprocess.run(['bash', '-c', check[2]], cwd=check[1], capture_output=True, text=True, timeout=90)
            got = re.findall(r'\d+', ran.stdout)
            if got and int(got[-1]) == int(check[3]):
                problems.append(f'Check already prints {check[3]} before any work: `{check[2]}` proves nothing; write a check that fails now')
        except subprocess.TimeoutExpired:
            problems.append('Check took over 90 s; done runs it too, so make it a focused count')
    for path, line in re.findall(r'(?<![\w.])(/\S+?\.\w+):(\d+)', body):
        if not os.path.exists(path) or int(line) > sum(1 for _ in open(path, errors='replace')):
            problems.append(f'cited {path}:{line} does not exist; read the code before citing it')
    # Ownership reversals and competing queued orders waste work.
    # One owner through completion; a change of owner names the concrete failure, and a
    # change of direction to the same unit names the order it replaces.
    held = [(u, d['msg']) for u, owed in load(DEADLINES, {}).items() for d in owed if set(facts(d)) & set(group)]
    holders = [(u, m) for u, m in held if u != unit]
    mine = [m for u, m in held if u == unit]
    if fact and holders and not reassign:
        problems.append(f'fact already owned by {holders}; keep that owner, or pass --reassign "<the concrete failure: provider diagnostic, strikes, missed deadline>"')
    if reassign is not None and len(reassign.split()) < 8:
        problems.append('--reassign needs the concrete failure that forces replacement (8+ words), told to the old and new owner')
    if fact and mine and replaces not in mine:
        problems.append(f'{unit} already holds {mine} on this problem; pass --replaces <that id> so the old order is withdrawn, never queue a competing one')
    if problems:
        sys.exit('Refused order:\n  ' + '\n  '.join(problems))
    struck = [s for s in load_strikes() if s['unit'] == unit and s.get('task') == fact]
    if fact and len(struck) >= 2 and not mechanical:
        sys.exit(f'Refused: {unit} has {len(struck)} strikes on {fact}. Reassign {fact}; only --mechanical orders on it reach {unit}.')
    sent = post([unit], f'{body.rstrip()}' + (f'\nThis replaces my order {replaces}; drop that one.' if replaces else '') + (f'\nTaken over because: {reassign}' if reassign else '') + '\n' + storage.RULE.replace('<you>', unit) + '\nIf two lines of this order conflict, take the narrower reading, say which one you took, and proceed; do not stop to ask.' + f'\nDue {due} {time.strftime("%Z")}. Miss it and the task is reassigned.')
    deadlines = load(DEADLINES, {})
    # One unit can owe several orders; each keeps its own deadline.
    key = group[0]
    if reassign:
        for other, owed in list(deadlines.items()):
            if other != unit and any(set(facts(d)) & set(group) for d in owed):
                taken = [d for d in owed if set(facts(d)) & set(group)]
                deadlines[other] = [d for d in owed if not set(facts(d)) & set(group)]
                # A Claude fork holds its order as claude-agent-<id>; it has no bus thread, I stop it myself.
                if all(d['msg'].startswith('claude-agent-') for d in taken):
                    continue
                post([other], f'Taken off you: {", ".join(group)} ("{load(PROBLEMS, {})[group[0]]["text"][:120]}") now belongs to {unit} because: {reassign}. Stop that work; do not touch its files.')
    if replaces:
        deadlines[unit] = [d for d in deadlines.get(unit, []) if d['msg'] != replaces]
    deadlines.setdefault(unit, []).append(dict(due=due, msg=sent, at=time.time(), fact=key, facts=group,
                                               check=[check[1], check[2], int(check[3])], disk=disk))  # disk: this order may allocate under the floor
    save(DEADLINES, deadlines)
    print(sent)
    if replaces and not clean:
        # A send-back is a strike, no warnings, and every send-back is roasted in #comms.
        # Done here so the commander cannot send work back and forget either.
        verdict = next(l for l in body.splitlines() if BLUNT.search(l)).strip()
        strike(unit, f'sent back on {group[0]}: {verdict}')


def say(units, body_file):
    body = open(body_file).read()
    # Numbered edits or a report line are an order: it needs a deadline, so it goes through send.
    if len(re.findall(r'(?m)^\s*\d+[.)] ', body)) >= 2 or re.search(r'(?mi)^\s*(report|due)\b.*:', body):
        sys.exit('Refused: a numbered edit list or a Report/Due line is an order. Use `order send` so it gets a tracked deadline.')
    refuse_dogma(body)
    refuse_slop_love(body)
    Slop.from_order(body)
    print(post(units.split(','), body.rstrip()))


def drop(unit, msg, by):
    deadlines = load(DEADLINES, {})
    owed = deadlines.get(unit, [])
    if not any(d['msg'] == msg for d in owed):
        sys.exit(f'Refused: {unit} owes no order {msg}')
    newer = next((d for d in owed if d['msg'] == by), None)
    old = next(d for d in owed if d['msg'] == msg)
    if newer is None or newer['at'] <= old['at']:
        sys.exit(f'Refused: {by} is not a newer open order to {unit}; only a live newer order replaces an old one')
    deadlines[unit] = [d for d in owed if d['msg'] != msg]
    save(DEADLINES, deadlines)
    print(f'{unit}: {msg} replaced by {by}')


def problem(text, where, unit, due, order_file, disk=False):
    """A problem is born owned: it is logged and its order sent in one step, or not at all."""
    body = open(order_file).read()
    if 'Problem: NEW' not in body:
        sys.exit('Refused: the order file needs a "Problem: NEW" line; a problem is never logged without its order')
    path, _, line = where.rpartition(':')
    if not os.path.exists(path) or not line.isdigit() or int(line) > sum(1 for _ in open(path)):
        sys.exit(f'Refused: {where} is not a real file:line; find where the problem lives first')
    if len(text.split()) < 8:
        sys.exit('Refused: describe the problem (8+ words): what is wrong, what it costs')
    problems = load(PROBLEMS, {})
    closed = [json.loads(l)['id'] for l in open(f'{RTS}/closed.jsonl') if '"id"' in l]
    pid = f'P{max([int(k[1:]) for k in (*problems, *closed) if k[1:].isdigit()] + [0]) + 1}'
    problems[pid] = dict(text=text, where=where, at=time.time())
    save(PROBLEMS, problems)
    open(order_file, 'w').write(body.replace('Problem: NEW', f'Problem: {pid}'))
    try:
        send(unit, due, order_file, disk=disk)
    except SystemExit:
        problems.pop(pid)
        save(PROBLEMS, problems)
        open(order_file, 'w').write(body)
        raise
    board()
    print(pid)


def close(pid, works, artifact):
    """Core owns closing: the installed evidence, the artifact and the no-open-order rule live on the Todo."""
    ThreadCaller(ME).invoke(CommsProblemCloseTool(problem=pid, works=works, artifact=artifact), COMMS)
    board()
    print(f'{pid} closed')


def fold(pid, into, why):
    """A duplicate row joins the problem that already states it; its text moves there, nothing is lost."""
    problems = load(PROBLEMS, {})
    if pid not in problems or into not in problems or pid == into:
        sys.exit(f'Refused: {pid} and {into} must be two different open problems')
    if len(why.split()) < 8:
        sys.exit('Refused: say why they are the same fact (8+ words)')
    if any(pid in facts(d) for owed in load(DEADLINES, {}).values() for d in owed):
        sys.exit(f'Refused: {pid} has open orders; link them to {into} first')
    gone = problems.pop(pid)
    problems[into]['text'] += f' [+{pid}: {gone["text"]}]'
    save(PROBLEMS, problems)
    with open(f'{RTS}/closed.jsonl', 'a') as f:
        f.write(json.dumps(dict(gone, id=pid, folded_into=into, why=why, closed=time.time())) + '\n')
    board()
    print(f'{pid} folded into {into}')


def last_activity(unit):
    lines = open(ACTIVITY).read().splitlines()[-6000:]
    stamps = [l[:8] for l in lines if l[9:].startswith(unit + ' ')]
    return stamps[-1] if stamps else None


def ownership():
    """Every open problem has an owner with an open order, and that owner is acting."""
    orders = {pid: (u, d) for u, owed in load(DEADLINES, {}).items() for d in owed for pid in facts(d)}
    delivered = {r.get('fact') for r in map(json.loads, open(DONE)) if r.get('fact')} if os.path.exists(DONE) else set()
    rows = []
    for pid, p in sorted(load(PROBLEMS, {}).items(), key=lambda kv: int(kv[0][1:])):
        # A delivered problem stays on the board until it is seen working in the installed app.
        unit, d = orders.get(pid, ('merged, install pending', None) if pid in delivered else (None, None))
        rows.append((pid, p, unit, d, last_activity(unit) if unit else None))
    return rows


def board():
    lines = ['# Fleet board', '',
             'GENERATED by agent-comms-commander order from problems.json and deadlines.json. Do not edit by hand:',
             '`order problem` adds a row, `order send ... Problem: P<n>` gives it an owner, `order close` removes it.', '',
             '| Problem | Where | Owner | Order | Due | Owner last acted |', '|---|---|---|---|---|---|']
    for pid, p, unit, d, seen in ownership():
        lines.append(f'| {pid}: {p["text"]} | {p["where"]} | {unit or "**NO OWNER**"} | {d["msg"] if d else "-"} | '
                     f'{d["due"] if d else "-"} | {seen or "-"} |')
    open(BOARD, 'w').write('\n'.join(lines) + '\n')


def link(unit, msg, pid):
    """Attach an order sent before the problem list existed to its problem."""
    deadlines = load(DEADLINES, {})
    order = next((d for d in deadlines.get(unit, []) if d['msg'] == msg), None)
    if order is None or pid not in load(PROBLEMS, {}):
        sys.exit(f'Refused: {unit} owes no order {msg}, or {pid} is not open')
    holders = [u for u, owed in deadlines.items() for d in owed if pid in facts(d) and u != unit]
    if holders:
        sys.exit(f'Refused: {pid} is already owned by {holders}')
    order['facts'] = [*facts(order), *([] if pid in facts(order) else [pid])]
    order.setdefault('fact', pid)
    save(DEADLINES, deadlines)
    board()
    print(f'{pid} owned by {unit} via {msg}')


def load_strikes():
    if not os.path.exists(STRIKES):
        return []
    with open(STRIKES) as f:
        return [json.loads(line) for line in f if line.strip()]


STAGES = ('tool call', 'review', 'install', 'live', 'user')  # where a mistake was caught; later is worse


def strike(unit, why, caught=f'{ME} at review'):
    """HARD RULE: every failure goes on the public record, in plain words, damning and exact.

    The strike is written to the narrative file and posted to #comms in the same step, so no
    failure stays private and none is described in words that hide what happened.
    """
    if len(why.split()) < 10:
        sys.exit('Refused: say what the unit actually did, plainly, in a full sentence (10+ words).')
    if jargon := jargon_in(why):
        sys.exit(f'Refused: jargon {jargon} hides what happened. Say it the way you would tell the user.')
    # What matters is how a mistake happened, how often, and how and when it was caught.
    if not any(stage in caught for stage in STAGES):
        sys.exit(f'Refused: --caught must say who caught it and at which stage {STAGES}, e.g. "{ME} at review".')
    task = (re.search(r'\bP\d+\b', why) or re.search('', '')).group(0) or None
    if task is None:
        sys.exit('Refused: name the task (P-number) the strike is for; strikes count per task.')
    with open(STRIKES, 'a') as f:
        f.write(json.dumps(dict(unit=unit, task=task, why=why, caught=caught, at=time.time())) + '\n')
    n = len([s for s in load_strikes() if s['unit'] == unit and s.get('task') == task])
    line = (f'{time.strftime("%Y-%m-%d %H:%M")} {unit} (strike {n} on {task}{f", {task} taken away" if n >= 2 else ""}; '
            f'caught by {caught}): {why}')
    new_file = not os.path.exists(NARRATIVE)
    with open(NARRATIVE, 'a') as f:
        if new_file:
            f.write('# What the fleet actually did\n\nEvery failure, in plain words, as it happened. Written by agent-comms-commander order strike; never edited.\n\n')
        f.write(f'- {line}\n')
    try:
        post(['#comms'], f'On the record: {line}')
    except SystemExit as failed:  # the strike is already on the record; a rerun would strike twice
        print(f'Strike recorded, but the #comms post failed ({failed}). Repost with order.post; do not strike again.', file=sys.stderr)
    print(f'{unit}: {n} strike(s) on {task}' + (f'. Reassign {task} now and freeze its branch.' if n >= 2 else ''))


def done(unit, msg, sha, repo=None):
    deadlines = load(DEADLINES, {})
    order = next((d for d in deadlines.get(unit, []) if d['msg'] == msg), None)
    if order is None:
        sys.exit(f'Refused: {unit} owes no order {msg}')
    operation = sha == 'op'  # an operation on live state, not a commit: the check runs now and now is delivery
    if not operation and not re.fullmatch(r'[0-9a-f]{7,40}|#\d+', sha):
        sys.exit('Refused: --sha must be the pushed commit (or #PR) that delivers the order')
    if 'check' in order:
        check_repo, command, want = order['check']
        repo = repo or check_repo  # a named checkout holding the delivered commit runs the same check
        # The check proves the delivered commit only when it runs on that commit, clean.
        head = ['x', 'x'] if operation else subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD', sha.lstrip('#') + '^{commit}'], capture_output=True, text=True).stdout.split()
        if len(head) != 2 or head[0] != head[1]:
            sys.exit(f'Refused: {repo} HEAD is {head[:1]}, not {sha}. Check out the delivered commit there (or name the checkout that has it) so the check runs on it.')
        if not operation and subprocess.run(['git', '-C', repo, 'status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True).stdout.strip():
            sys.exit(f'Refused: {repo} has uncommitted changes; the check would not be about {sha}.')
        if not operation:
            diff = subprocess.run(['git', '-C', repo, 'show', '--first-parent', '-m', '--format=', sha.lstrip('#')], capture_output=True, text=True).stdout
            changed = re.findall(r'^\+\+\+ b/(\S+\.py)$', diff, re.M)
            after = [subprocess.run(['git', '-C', repo, 'show', f"{sha.lstrip('#')}:{f}"], capture_output=True, text=True).stdout for f in changed]
            if added := forwarders_added(diff, after):
                sys.exit(f'Refused: {sha} adds forwarders {added[:5]}; slop never counts as delivery. Order the hierarchy fix.')
        ran = subprocess.run(['bash', '-c', command], cwd=repo, capture_output=True, text=True, timeout=120)
        got = re.findall(r'\d+', ran.stdout)
        if not got or int(got[-1]) != want:
            sys.exit(f'Refused: the order\'s own check `{command}` in {repo} printed {ran.stdout.strip()[-200:]!r}, wanted {want}. Not delivered.')
    trees, _ = storage.unit_paths(unit)
    if left := [t for t in trees if any(f.lower() in t.lower() for f in facts(order))]:
        sys.exit(f'Refused: {unit} still has the checkout {left} for this order; storage rule: it removes it before the order counts as done.')
    if quota := storage.over_quota(unit):
        sys.exit(f'Refused: {quota}; storage rule: it cleans up before the order counts as done.')
    if operation:
        delivered, delivered_at = time.strftime('%H:%M'), time.time()
    repo = repo or (order.get('check') or [None])[0]
    if repo is None:
        sys.exit('Refused: pass --repo so the delivery time comes from the commit, not from when I got round to it')
    stamp = None if operation else subprocess.run(['git', '-C', repo, 'show', '-s', '--format=%ct', sha.lstrip('#')], capture_output=True, text=True)
    if stamp is not None and stamp.returncode:
        sys.exit(f'Refused: {sha} is not a commit in {repo}: {stamp.stderr.strip()}')
    if stamp is not None:
        delivered_at = int(stamp.stdout.split()[0])
        delivered = time.strftime('%H:%M', time.localtime(delivered_at))
    deadlines[unit] = [d for d in deadlines[unit] if d is not order]
    if not deadlines.get(unit):
        deadlines.pop(unit, None)
    save(DEADLINES, deadlines)
    with open(DONE, 'a') as f:
        f.write(json.dumps({'unit': unit, 'msg': msg, 'fact': order.get('fact'), 'done': sha, 'at': delivered_at}) + '\n')
    # The deadline is the day's HH:MM; delivery after it is a strike, never a clean met.
    # An op has no commit time: recording it later is my delay, so lateness is judged at the OVERDUE alarm.
    if sha != 'op' and delivered_at > due_at(order):
        strike(unit, f'late on {order.get("fact")}: order {msg} due {order["due"]} delivered {delivered} as {sha}')
        return
    # A met deadline clears the unit's strikes: the record is per task.
    kept = [s for s in load_strikes() if not (s['unit'] == unit and s.get('task') == order.get('fact'))]
    with open(STRIKES, 'w') as f:
        f.writelines(json.dumps(s) + '\n' for s in kept)
    print(f'{unit}: order {msg} met as {sha}, strikes on {order.get("fact")} cleared')


ACTIVITY = f'{RTS}/activity.log'


def first_calls(deadlines):
    """Each order starts a first-tool-call clock: 2 minutes, separate from its commit deadline."""
    today = time.strftime('%Y-%m-%d')
    lines = open(ACTIVITY).read().splitlines()[-4000:]
    def acted_since(unit, at):
        since = time.strftime('%H:%M:%S', time.localtime(at))
        return any(line[9:].startswith(unit + ' ') and line[:8] >= since for line in lines)
    watched = {line[9:].split(' ', 1)[0] for line in lines}
    missing, changed = [], False
    for unit, owed in deadlines.items():
        if unit not in watched:
            continue  # tool_watch cannot see this unit's harness; its clock is the commit deadline
        for d in owed:
            if d.get('started') or 'at' not in d or time.strftime('%Y-%m-%d', time.localtime(d['at'])) != today:
                continue
            if acted_since(unit, d['at']):
                d['started'] = changed = True
            elif time.time() - d['at'] > 120 and not d.get('no_first_call'):
                d['no_first_call'] = changed = True
                missing.append(f'{unit} (order {d["msg"]}, sent {time.strftime("%H:%M", time.localtime(d["at"]))})')
    return missing, changed


HUNTS = f'{RTS}/hunts.jsonl'
HUNT_ROOTS = [(str(repo.checkout), repo.source) for repo in CONFIG.repos]


def hunt(where, why, sent=None):
    """A hunt is a hot-path read with a verdict: file:line, what scales or repeats, and the order it became (or --clean)."""
    path, _, line = where.rpartition(':')
    if not os.path.exists(path) or not line.isdigit() or int(line) > sum(1 for _ in open(path)):
        sys.exit(f'Refused: {where} is not a real file:line')
    if len(why.split()) < 10:
        sys.exit('Refused: say what the code does per event or per history row, and the fix or why it is clean (10+ words)')
    if sent and sent not in {d['msg'] for owed in load(DEADLINES, {}).values() for d in owed}:
        sys.exit(f'Refused: {sent} is not an open tracked order; send it first or mark the hunt --clean')
    with open(HUNTS, 'a') as f:
        f.write(json.dumps(dict(where=os.path.realpath(path) + ':' + line, why=why, sent=sent, at=time.time())) + '\n')
    print(f'hunt recorded: {where} -> {sent or "clean"}')


def next_hunt():
    """Least-recently hunted file among the most-churned production files on origin/main."""
    hunted = {}
    for h in (json.loads(l) for l in open(HUNTS)) if os.path.exists(HUNTS) else ():
        f = h['where'].rpartition(':')[0]
        hunted[f] = max(hunted.get(f, 0), h['at'])
    candidates = []
    for repo, root in HUNT_ROOTS:
        log = subprocess.run(['git', '-C', repo, 'log', 'origin/main', '--since=7.days', '--name-only', '--format=', '--', root],
                             capture_output=True, text=True).stdout.split()
        churn = {}
        for f in log:
            full = os.path.realpath(os.path.join(repo, f))
            if os.path.exists(full) and f.endswith('.py'):
                churn[full] = churn.get(full, 0) + 1
        candidates += sorted(churn, key=churn.get, reverse=True)[:15]
    return min(candidates, key=lambda f: hunted.get(f, 0)) if candidates else None


def replied(unit, since):
    """The unit's latest message to me after the order, so an overdue alarm is read against what it said."""
    with open(f'{B}/bus.jsonl', 'rb') as f:
        f.seek(max(0, os.path.getsize(f.name) - 3_000_000))
        lines = f.read().splitlines()[1:]
    latest = None
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        to = str(m.get('to'))
        if m.get('from') == unit and float(m.get('ts', 0)) > since and (ME in to or '#comms' in to):
            latest = m
    if latest is None:
        return '; no reply since the order'
    at = time.strftime('%H:%M', time.localtime(float(latest['ts'])))
    return f'; REPLIED {at} [{latest["id"]}]: {" ".join(str(latest.get("text")).split())[:160]} -- read it before pushing'


def due_at(order) -> float:
    """The deadline is the first HH:MM at or after the moment the order was sent."""
    hour, minute = map(int, order['due'].split(':'))
    sent = time.localtime(order['at'])
    due = time.mktime((sent.tm_year, sent.tm_mon, sent.tm_mday, hour, minute, 0, 0, 0, -1))
    return due + 86400 if due < order['at'] - 60 else due


def overdue():
    checked = 0
    while True:
        if time.time() - checked > 300:
            checked = time.time()
            if storage.free_gb() < storage.CLEANUP_GB:
                print(f'DISK LOW: {CONFIG.disk} has {storage.free_gb():.1f} GB free (cleanup due under {storage.CLEANUP_GB} GB) -> put a cleanup owner on it now', flush=True)
            heavy = [q for u in load(DEADLINES, {}) if (q := storage.over_quota(u))]
            if heavy:
                print('VIOLATION storage quota: ' + '; '.join(heavy) + ' -> order the cleanup with --disk', flush=True)
        deadlines = load(DEADLINES, {})
        missing, changed = first_calls(deadlines)
        if changed:
            save(DEADLINES, deadlines)
        if missing:
            print('NO FIRST CALL: ' + ', '.join(missing) + ' -> stuck holding an order; push it now or reassign',
                  flush=True)
        now = time.strftime('%H:%M')
        unowned = [pid for pid, p, unit, d, seen in ownership() if unit is None and time.time() - p['at'] > 300]
        if unowned:
            print(f'NO OWNER: {", ".join(unowned)} -> order send UNIT DUE FILE with Problem: line, now', flush=True)
        stale = [f'{pid} {unit} (last {seen})' for pid, p, unit, d, seen in ownership()
                 if unit and seen and seen < time.strftime('%H:%M:%S', time.localtime(time.time() - 1200))]
        if stale:
            print(f'IDLE OWNER 20+ min: {", ".join(stale)} -> push or reassign', flush=True)
        late = [f'{u} (due {d["due"]}, order {d["msg"]}{replied(u, d["at"])})' for u, owed in load(DEADLINES, {}).items()
                for d in owed if due_at(d) < time.time()]
        if late:
            print('OVERDUE: ' + ', '.join(late) + ' -> read the diff now; missed = order strike, 2 strikes = fire',
                  flush=True)
        # Units leave finished PRs open and wander off. An open PR naming an ordered problem that
        # nobody has touched for 5 minutes is a stall, whatever the unit says it is doing.
        ordered = {pid: (u, d['at'], due_at(d)) for u, owed in load(DEADLINES, {}).items() for d in owed for pid in facts(d)}
        release()
        idle_prs = []
        for repo in (repo.github for repo in CONFIG.repos):
            listed = subprocess.run(['gh', 'pr', 'list', '-R', repo, '--state', 'open', '--limit', '40',
                                     '--json', 'number,title,updatedAt'], capture_output=True, text=True, timeout=60)
            for pr in json.loads(listed.stdout or '[]'):
                touched = calendar.timegm(time.strptime(pr['updatedAt'], '%Y-%m-%dT%H:%M:%SZ'))
                for pid in re.findall(r'P\d+', pr['title']):
                    # An order sent after the last touch is the push; until its deadline, OVERDUE owns the alarm.
                    if pid in ordered and time.time() - touched > 300 and ordered[pid][1] < touched and time.time() > ordered[pid][2]:
                        idle_prs.append(f'{ordered[pid][0]} {repo.split("/")[1]}#{pr["number"]} ({pid})')
        if idle_prs:
            print('STALL PR open and untouched 5+ min: ' + ', '.join(idle_prs) + ' -> order the merge or the fix now', flush=True)
        hunts = [json.loads(l) for l in open(HUNTS)] if os.path.exists(HUNTS) else []
        if not hunts or time.time() - hunts[-1]['at'] > 900:
            print(f'HUNT DUE (none in 15 min): read {next_hunt()} for per-event or per-history work or slop, '
                  'then order hunt FILE:LINE --why "..." [--sent ORDER] (or --clean)', flush=True)
        release()
        time.sleep(60)


def main(a):
    from . import capacity
    who = ME
    if '--fork' in a:  # a forked commander: keeps its beat fresh and is never blocked by the overload gate
        n = a.index('--fork')
        who = a[n + 1]
        capacity.beat(who)
        del a[n:n + 2]
    elif a[:1] and a[0] in ('send', 'say', 'problem', 'hunt', 'fold'):
        capacity.require()  # HARD RULE: overloaded means fork, not more orders from me
    if a[:1] and a[0] in ('send', 'strike', 'done', 'drop', 'say', 'problem', 'close', 'fold', 'hunt'):
        from . import rules
        rules.require(who)  # HARD RULE: no commander action, mine or a fork's, on rules not re-read in 20 min
    if a[:1] == ['send'] and len(a) >= 4:
        opt = lambda k: a[a.index(k) + 1] if k in a[4:-1] else None
        send(*a[1:4], clean='--clean' in a[4:], mechanical='--mechanical' in a[4:], reassign=opt('--reassign'), replaces=opt('--replaces'), disk='--disk' in a[4:])
    elif a[:1] == ['strike'] and len(a) == 6 and a[2] == '--why' and a[4] == '--caught':
        strike(a[1], a[3], a[5])
    elif a[:1] == ['strike'] and len(a) == 4 and a[2] == '--why':
        strike(a[1], a[3])
    elif a[:1] == ['done'] and len(a) in (5, 7) and a[3] == '--sha' and (len(a) == 5 or a[5] == '--repo'):
        done(a[1], a[2], a[4], a[6] if len(a) == 7 else None)
    elif a[:1] == ['drop'] and len(a) == 5 and a[3] == '--by':
        drop(a[1], a[2], a[4])
    elif a[:1] == ['say'] and len(a) == 3:
        say(a[1], a[2])
    elif a[:1] == ['problem'] and len(a) in (10, 11) and a[2] == '--where' and a[4] == '--to' and a[6] == '--due' and a[8] == '--order' and a[10:] in ([], ['--disk']):
        problem(a[1], a[3], a[5], a[7], a[9], disk=a[10:] == ['--disk'])
    elif a[:1] == ['close'] and len(a) == 6 and a[2] == '--works' and a[4] == '--artifact':
        close(a[1], a[3], a[5])
    elif a[:1] == ['fold'] and len(a) == 6 and a[2] == '--into' and a[4] == '--why':
        fold(a[1], a[3], a[5])
    elif a[:1] == ['link'] and len(a) == 4:
        link(*a[1:])
    elif a[:1] == ['hunt'] and len(a) in (4, 6) and a[2] == '--why' and (len(a) == 4 or a[4] == '--sent'):
        hunt(a[1], a[3], a[5] if len(a) == 6 else None)
    elif a[:1] == ['next-hunt']:
        print(next_hunt())
    elif a[:1] == ['board']:
        board()
    elif a[:1] == ['overdue']:
        overdue()
    else:
        sys.exit('REFUSED, nothing recorded. Usage:\n' + __doc__.split('\n\n')[0])
