"""The only way the commander sends an order to a unit. Refuses soft or vague orders; tracks deadlines and strikes.

`order send` bodies declare, on their own lines:
  Problems: P<n>[, P<m>...]                       (the listed problems it owns, batched by surface for one owner; one open order per problem)
  Owner: <path>:<line>                              (the existing owner the fix goes on; must exist)
  Check: <repo> :: <shell command> -> <integer>    (done runs it; the last integer printed must match)
  Works when: <user-visible closing run, 8+ words> (unless mechanical; close requires the file)
  Deletes: <decision family> at PATH:LINE :: <repo> :: <family-wide count>
    (optional; done refuses unless the count is 0)
  1. <edit> ...  Banned: <shape>; <shape>  Report: <what the unit sends back>
Sending to the unit that already holds the problem replaces its order; without --clean that is a send-back strike.

Incompetence is not tolerated: a unit is fired from a task, not berated about it.
Two strikes on one task: the task is taken away and reassigned (`fire`). A strike recorded in
error is voided with `unstrike`, never deleted: it stays readable beside its void and stops
counting. An order sent in error is retired with `withdraw`: its problem closes with no strike.
Nothing here judges code; it refuses orders that leave a unit room to interpret,
and refuses to let a deadline lapse silently.
"""
import argparse, asyncio, json, os, re, subprocess, sys, time
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..change_report import FileLine
from ..child_process import BoundedRun
from ..cli_commands import DeclaredOptions, option
from ..declared_family import DeclaredFamily
from ..comms import Comms
from ..goal_actions import CreateProblemGoalAction, OrderGoalAction
from ..goals import CheckPrinted, OrderCheck, ProblemGoalOrder
from ..native_tool_call import SelectedToolDenied
from ..plan_phases import OrderPhase, UnplannedPhase
from ..recorded_time import RecordedEventTime
from ..standing_rules import HuntDue, RulesDue
from ..tool_caller import ThreadCaller
from ..tools import (CommsFireTool, CommsOrderCheckTool,
                     CommsOrderServeTool, CommsOrderDoneTool, CommsOrderTool,
                     CommsProblemCloseTool, CommsTodoCreateTool, CommsTodoListTool, CommsTodoTool, TodoList)
from ..todos import (ApplicationOrderRun, InstallDeadline, PlannedInstallDeadline, OrderMessage, MechanicalOrderRun, OrderRun, BluntText, CommitDelivery, FairText, HierarchyText, NotifiedFirstCallEvent, ObservedFirstCallEvent,
                     OperationDelivery, PlainText, CleanHunt, OrderedHunt, PendingProblemOrder, RecordFirstCallTodo,
                     OrderDeletion, RecordHuntTodo, StrikeReason, StrikeTodo, VoidStrikeTodo,
                     WithdrawTodo, AssignTodo, OrderProgress, Todo)
from . import capacity
from .config import CommanderConfig, Repo

CONFIG = CommanderConfig.installed()
ME = CONFIG.commander
COMMS = Comms(CONFIG.root)  # the bus whose Todo store owns problems, orders, deadlines and strikes
RTS = str(CONFIG.state)
BOARD = str(CONFIG.board)
NARRATIVE = str(CONFIG.narrative)  # public record of every failure, in plain words
B = str(CONFIG.root)
CLI = str(CONFIG.cli)
OUTGOING = (FairText, HierarchyText, PlainText)  # every commander text passes each one

def refuse(text, *families):
    """Core's text families decide what the commander may write; nothing is recorded when one refuses."""
    if refused := [r for family in families if (r := family.refused(text))]:
        sys.exit('REFUSED, nothing recorded: ' + ' '.join(refused))


def post(recipients, body):
    """Send one message its caller refused with OUTGOING; judge reads it from bus history."""
    command = ['timeout', '300', CLI, '--root', B, 'send', '--from', ME]
    for unit in recipients:
        command += ['--to', unit]
    out = subprocess.run(command + ['--type', 'info', '--body', body], capture_output=True, text=True)
    if '"id"' not in out.stdout:
        sys.exit(f'Send failed: {out.stdout}{out.stderr}')
    return json.loads(out.stdout)['id']


def call(tool):
    """Every order write is one Core tool request made as the commander's thread."""
    return ThreadCaller(ME).invoke(tool, COMMS)


def todos():
    """The problems this commander created; Core's Todo store is their one owner."""
    return COMMS.goals.todos.for_commander(COMMS.registry.require(ME).incarnation)


def held_orders() -> tuple[Todo, ...]:
    """This commander's open orders that a unit holds."""
    return COMMS.goals.todos.held_for_commander(COMMS.registry.require(ME).incarnation)


def held_by(unit: str) -> tuple[Todo, ...]:
    """This commander's open orders held by one unit."""
    return COMMS.goals.todos.held_by(COMMS.registry.require(ME).incarnation, unit)


def progress() -> dict[str, OrderProgress]:
    """Each held order's progress, derived by Core's Todo list from the unit's checkout and PRs."""
    listed: TodoList = call(CommsTodoListTool())
    return listed.progress


def struck(todo, unit):
    """Strikes count per unit and task, across every order of that task the unit held."""
    return len(todo.live_strikes(unit))


def hhmm(event):
    return time.strftime('%H:%M', time.localtime(event.at))


def deadline(due):
    """HH:MM local time: the first such minute at or after now."""
    hour, minute = map(int, due.split(':'))
    now = time.time()
    day = time.localtime(now)
    at = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, hour, minute, 0, 0, 0, -1))
    return RecordedEventTime(at + 86400 if at < now - 60 else at)


# Eliminate forwarding with inheritance and multiple parents; decompose the hierarchy if it is not sound.
# A forwarder restates an implementation pair (paper 1 Fig 3: fields + forwarding leave routes to maintain), so
# nothing the commander approves, delivers or orders may add or keep one. The cure is the hierarchy, never a
# route: fold the false owner into the real one, or compose the providers as parents.
FORWARDER = re.compile(
    r"^\+\s*(?:def \w+\([^)]*\)(?:\s*->[^:]+)?:\s*return self\.\w+\.\w+(?:\([^()]*\))?\s*$"      # one-line def returning self.x.y(...)
    r"|\w+\s*=\s*property\(lambda \w+:\s*\w+\.\w+\.\w+\)"                                        # attr = property(lambda s: s._a.attr)
    r"|def __getattr__\(self)", re.M)
FORWARD_BODY = re.compile(r"^\+(\s*)def (\w+)\(self[^)]*\)[^:]*:\s*\n\+\1\s+return self\.\w+\.\2\b", re.M)  # def x(self): / return self._a.x(...)
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


def send(unit, due, body_file, request, clean=False, mechanical=False, reassign=None, new=None, traced=None):
    """Validate an order body and record it in Core: a new problem (new=(PID, TEXT)) or the listed open problems.

    Sending to a unit that already holds the problem replaces its order there; without --clean that is a send-back."""
    body = open(body_file).read()
    problems = []
    # Below the floor Core's StorageRule denies each allocating tool call, so the order prose is not checked.
    try:
        COMMS.registry.storage_rule.require_quota(COMMS.registry.require(unit))
    except SelectedToolDenied as quota:
        problems.append(f'{unit}: {quota}: it cleans up its own checkouts and scratch before it gets new work')
    Slop.from_order(body)  # calling something slop requires the fact, where it is restated, its owner and the mechanism
    if not clean and (timid := BluntText.refused(body)):  # blunt at all times unless nothing was incompetent
        problems.append(timid)
    works = re.search(rf'(?m)^{re.escape(OrderRun.label)}\s*(.+)$', body)
    try:
        run = MechanicalOrderRun() if mechanical else ApplicationOrderRun(works[1] if works else '')
    except ValueError as refused:
        problems.append(str(refused))
    serves = re.search(r'(?m)^Serves:\s*(\S+)\s*$', body)
    try:
        phase = OrderPhase.member_named(serves[1] if serves else UnplannedPhase.declared_name)
    except ValueError as refused:
        problems.append(f'{refused}; Serves: names one of {", ".join(OrderPhase.names())}')
    edits = tuple(re.findall(r'(?m)^\s*\d+[.)] (.+?)\s*$', body))
    if not edits:
        problems.append('no numbered edits: list each exact edit as "1. ...", "2. ..."')
    report = re.search(r'(?m)^Report:\s*(\S.*?)\s*$', body)
    if not report:
        problems.append('no Report: line: say exactly what the unit sends back (sha, counts before -> after)')
    banned = tuple(item.strip() for line in re.findall(r'(?m)^Banned:(.*)$', body) for item in line.split(';') if item.strip())
    if not banned:
        problems.append('no Banned: line: name the slop shapes this task invites, separated by ";"')
    if not re.fullmatch(r'\d{1,2}:\d{2}', due):
        problems.append('due must be HH:MM local time')
    fact = re.search(r'(?m)^Problems?:\s*(P\d+(?:\s*,\s*P\d+)*)\b', body)
    group = [new[0]] if new else re.findall(r'P\d+', fact[1]) if fact else []
    owner = re.search(r'(?m)^Owner:\s*(\S+):(\d+)\s*$', body)
    check = (line := re.search(r'(?m)^Check:(.*)$', body)) and OrderCheck.parse(line[1])
    listed = {t.id: t for t in todos()}
    if not group:
        problems.append('no Problem: P<n> line; record it first with `order problem` (every order owns a listed problem)')
    elif not new and (missing := [pid for pid in group if pid not in listed]):
        problems.append(f'{missing} not open problems')
    if not owner:
        problems.append('no Owner: path:line of the existing owner the fix belongs on')
    elif not os.path.exists(owner[1]) or int(owner[2]) > sum(1 for _ in open(owner[1])):
        problems.append(f'Owner {owner[1]}:{owner[2]} does not exist; read the code and name the real owner')
    if not check:
        problems.append('no Check: <repo> :: <command> -> N; done runs it, so make it the real acceptance count')
    elif not os.path.isdir(check.directory):
        problems.append(f'Check repo {check.directory} is not a directory')
    # A check that can never print its target, or a premise naming lines that do something else, wastes the order.
    # An order's check must fail now (or it proves nothing), and every absolute
    # path:line it cites must exist.
    if check and os.path.isdir(check.directory) and not problems:
        try:
            ran = subprocess.run(['bash', '-c', check.command], cwd=check.directory, capture_output=True, timeout=90)
            printed = CheckPrinted.decoded(ran.stdout)
            problems += printed.unproven(check.command, check.result.expected)
        except subprocess.TimeoutExpired:
            problems.append('Check took over 90 s; done runs it too, so make it a focused count')
    for path, line in re.findall(r'(?<![\w.])(/\S+?\.\w+):(\d+)', body):
        if not os.path.exists(path) or int(line) > sum(1 for _ in open(path, errors='replace')):
            problems.append(f'cited {path}:{line} does not exist; read the code before citing it')
    # A module deleted here can still be imported by another repo:
    # P12 deleted acp_ingress.py, which Toad imports.
    deleted = {stem for edit in edits
               for stem in re.findall(r'(?i)\bdelete\s+`?(?:\S*/)?(\w+)\.py\b', edit)}
    if check and deleted and (unread := importers(deleted, check)):
        problems.append(f'the order deletes {sorted(deleted)}, which {unread} import; '
                        'the check must also count their `from agent_comms` imports')
    if any(re.search(r'(?<![\w/])tools/', edit) for edit in edits) and not all(
            said in body for said in ('--root tools', '--root src')):
        problems.append('the order changes tools/: say "ratchet with --root tools and with '
                        '--root src, each as its own command"')
    # Ownership reversals waste work: one owner through completion; a change of owner names the concrete failure.
    holders = sorted({listed[pid].holder for pid in group if pid in listed} - {unit, None})
    replaces = not new and all(pid in listed and listed[pid].holder == unit for pid in group)
    try:
        trace = FileLine.from_wire(traced) if traced is not None else None
        for pid in group:
            if pid in listed and listed[pid].assignment:
                listed[pid].assignment.require_trace(unit, trace)
    except ValueError as refused:
        problems.append(str(refused))
    if holders and not reassign:
        problems.append(f'fact already owned by {holders}; keep that owner, or pass --reassign "<the concrete failure: provider diagnostic, strikes, missed deadline>"')
    if reassign is not None and len(reassign.split()) < 8:
        problems.append('--reassign needs the concrete failure that forces replacement (8+ words), told to the old and new owner')
    if problems:
        sys.exit('Refused order:\n  ' + '\n  '.join(problems))
    message = (f'{body.rstrip()}' + ('\nThis replaces my earlier order on it; drop that one.' if replaces else '') + (f'\nTaken over because: {reassign}' if reassign else '') + '\n' + COMMS.registry.storage_rule.order_text(unit, [f'{repo.checkout}/.venv/bin/python' for repo in CONFIG.repos]) + '\nIf two lines of this order conflict, take the narrower reading, say which one you took, and proceed; do not stop to ask.'
               + '\nStart now and push as you go.')  # a unit shown a due time takes until then
    taken = {other: f'Taken off you: {", ".join(group)} ("{listed[group[0]].text[:120]}") now belongs to {unit} because: {reassign}. Stop that work; do not touch its files.' for other in holders}
    refuse('\n'.join((message, *taken.values())), *OUTGOING)  # before Core: refused records nothing
    fired = [pid for pid in group if pid in listed and listed[pid].struck_out(unit)]
    if not mechanical and fired:
        sys.exit(f'Refused: {unit} has {ProblemGoalOrder.StrikeLimit} strikes on {fired}. '
                 f'Reassign them; only --mechanical orders on them reach {unit}.')
    # A miss already struck on the order this replaces is one miss:
    # the send-back records no second strike.
    struck_already = replaces and listed[group[0]].struck_on_order(unit)
    at = time.time()
    try:
        order = PendingProblemOrder(
            id=uuid4().hex[:12], owner=FileLine(owner[1], int(owner[2])),
            check=check, edits=edits, banned=banned,
            report=report[1], run=run, serves=phase,
            deletes=OrderDeletion.parse(deletes[1] if (deletes := re.search(
                rf'(?m)^{re.escape(OrderDeletion.label)}\s*(.+)$', body)) else None),
            at=RecordedEventTime(at), first_call_deadline=RecordedEventTime(at + 120))
    except ValueError as refused:  # Core owns what an order may say
        sys.exit(f'Refused order: {refused}')
    common = dict(order=order, request=request, deadline=deadline(due), commander=ME)
    if new:
        call(CommsTodoCreateTool(owner=unit, order=CreateProblemGoalAction(
            problem=new[0], repo=Repo.of(Path(check.directory)).github, text=new[1], **common)))
    for pid in [] if new else group:
        call(CommsOrderTool(owner=unit, order=OrderGoalAction(
            problem=pid, expected_revision=COMMS.goals.todos.get(pid).revision, traced=trace, **common)))
    message += "\n" + "\n".join(order.pull_request_instruction(pid) for pid in group)
    sent = post([unit], message)
    # The message is each order's recorded link; a verdict citing it lands on that order.
    for pid in group:
        call(CommsTodoTool(id=pid, operation=OrderMessage(
            COMMS.goals.todos.get(pid).revision, COMMS.registry.require(ME).incarnation, sent)))
    for other, text in taken.items():
        post([other], text)
    print(order.id)
    if replaces and not clean and struck_already:
        print(f'{unit} already struck on this order of {group[0]}; '
              'the send-back counts that one miss once')
    elif replaces and not clean:
        # A send-back is a strike, no warnings, and every send-back is roasted in #comms.
        # Done here so the commander cannot send work back and forget either.
        verdict = next(l for l in body.splitlines() if BluntText.found(l)).strip()
        strike(unit, f'sent back on {group[0]}: {verdict}')


def importers(modules: set[str], check: OrderCheck) -> list[str]:
    """The other configured repos whose `from agent_comms` imports name a module, unless the
    check reads them."""
    pattern = rf'^\s*(from|import) agent_comms\b.*\b({"|".join(sorted(modules))})\b'
    return [str(repo.checkout) for repo in CONFIG.repos
            if repo.checkout != Path(check.directory).resolve()
            and str(repo.checkout) not in check.command
            and subprocess.run(['grep', '-rqE', '--include=*.py', pattern,
                                str(repo.checkout / repo.source)]).returncode == 0]


def correct_check(pid: str, line: str, why: str) -> None:
    """Core replaces a wrong check of an open order. The new check must already pass on main, so a
    correction fixes the commander's check and never passes undelivered work: done still runs it on
    the --sha it is given."""
    todo = COMMS.goals.todos.get(pid)
    if not todo.state.open or not todo.pending:
        sys.exit(f'Refused: {pid} has no pending order')
    if not (check := OrderCheck.parse(line)):
        sys.exit('Refused: --check reads "<repo> :: <shell command> -> <integer>"')
    if not os.path.isdir(check.directory):
        sys.exit(f'Refused: check repo {check.directory} is not a directory')
    if len(why.split()) < 8:
        sys.exit('Refused: --why says what was wrong with the recorded check (8+ words)')
    def git(*args: str) -> str:
        return subprocess.run(['git', '-C', check.directory, *args],
                              capture_output=True, text=True).stdout.strip()
    head = git('rev-parse', 'HEAD')
    if head != git('rev-parse', 'origin/main') or git('status', '--porcelain', '-uno'):
        sys.exit(f'Refused: {check.directory} is not a clean checkout of origin/main; '
                 'the corrected check runs on main')
    try:  # the result done requires, judged on main
        check.result.require(asyncio.run(BoundedRun.run(
            ('bash', '-c', check.command), cwd=check.directory, timeout=90)))
    except ValueError as failed:
        sys.exit(f'Refused on main {head[:9]}: {failed} '
                 'order check corrects a wrong check; it does not pass undelivered work')
    call(CommsOrderCheckTool(problem=pid, check=check, why=why))
    print(f'{pid}: order {todo.order.id} now checks `{check.command}`')


def say(units, body_file):
    body = open(body_file).read()
    # Numbered edits or a report line are an order: it needs a deadline, so it goes through send.
    if len(re.findall(r'(?m)^\s*\d+[.)] ', body)) >= 2 or re.search(r'(?mi)^\s*(report|due)\b.*:', body):
        sys.exit('Refused: a numbered edit list or a Report/Due line is an order. Use `order send` so it gets a tracked deadline.')
    refuse(body, *OUTGOING)
    Slop.from_order(body)
    print(post(units.split(','), body.rstrip()))


def problem(text, unit, due, order_file, request):
    """A problem is born owned: Core records it with its order in one step, or not at all."""
    body = open(order_file).read()
    if 'Problem: NEW' not in body:
        sys.exit('Refused: the order file needs a "Problem: NEW" line; a problem is never logged without its order')
    if len(text.split()) < 8:
        sys.exit('Refused: describe the problem (8+ words): what is wrong, what it costs')
    history = COMMS.goals.todos.history_for_commander(COMMS.registry.require(ME).incarnation)
    pid = f'P{max([int(t.id[1:]) for t in history if t.id[1:].isdigit()] + [0]) + 1}'
    open(order_file, 'w').write(body.replace('Problem: NEW', f'Problem: {pid}'))
    try:
        send(unit, due, order_file, request, new=(pid, text))
    except SystemExit:
        open(order_file, 'w').write(body)
        raise
    board()
    print(pid)


def close(pid, works, artifact):
    """Core owns closing: the installed evidence, the artifact and the no-open-order rule live on the Todo."""
    ThreadCaller(ME).invoke(CommsProblemCloseTool(problem=pid, works=works, artifact=artifact), COMMS)
    board()
    print(f'{pid} closed')


def last_activity(unit):
    lines = open(ACTIVITY).read().splitlines()[-6000:]
    stamps = [l[:8] for l in lines if l[9:].startswith(unit + ' ')]
    return stamps[-1] if stamps else None


def board():
    lines = ['# Fleet board', '',
             "GENERATED by agent-comms-commander order from Core's Todo store. Do not edit by hand:",
             '`order problem` adds a row, `order send ... Problem: P<n>` orders it, `order close` or `order withdraw` removes it.', '',
             '| Problem | Owner file | Owner | Order | Due | Progress | Delivered '
             '| Owner last acted |',
             '|---|---|---|---|---|---|---|---|']
    listed, read = todos(), progress()
    for t in sorted(listed, key=lambda t: int(t.id[1:]) if t.id[1:].isdigit() else 0):
        unit, last = t.holder, t.released[-1:]
        owner = unit or f'unassigned: released from {last[0].previous.owner}: {last[0].reason.text}'
        lines.append(f'| {t.id}: {t.text} | {t.order.owner.to_wire()} | {owner} | {t.order.id} | '
                     f'{hhmm(t.deadline)} | {read[t.id].line if t.id in read else "-"} | '
                     f'{"no" if t.pending else "yes, install pending"} | '
                     f'{unit and last_activity(unit) or "-"} |')
    history = COMMS.goals.todos.history_for_commander(COMMS.registry.require(ME).incarnation)
    lines += ['', '## Withdrawn orders', '',
              *(f'- {line}' for t in history for line in t.order.withdrawals(t))]
    open(BOARD, 'w').write('\n'.join(lines) + '\n')


STAGES = ('tool call', 'review', 'install', 'live', 'user')  # where a mistake was caught; later is worse


def on_record(line: str, recorded: str, audience: tuple[str, ...] = ('#comms',)) -> None:
    """Put one line on the public record: the narrative file, then its audience.

    Core already holds what `recorded` names, so a failed post is reposted, never recorded again.
    """
    new_file = not os.path.exists(NARRATIVE)
    with open(NARRATIVE, 'a') as f:
        if new_file:
            f.write('# What the fleet actually did\n\nEvery failure, in plain words, as it happened. '
                    'Written by agent-comms-commander order; never edited.\n\n')
        f.write(f'- {line}\n')
    try:
        post([*audience], f'On the record: {line}')
    except SystemExit as failed:
        print(f'{recorded} recorded, but the post failed ({failed}). '
              'Repost with order.post; do not record it again.', file=sys.stderr)


def strike(unit, why, caught=f'{ME} at review'):
    """HARD RULE: every failure goes on the public record, in plain words, damning and exact.

    Core records the strike on the task's Todo; the strike is written to the narrative file and posted to
    #comms in the same step, so no failure stays private and none is described in words that hide what happened.
    """
    # What matters is how a mistake happened, how often, and how and when it was caught.
    if not any(stage in caught for stage in STAGES):
        sys.exit(f'Refused: --caught must say who caught it and at which stage {STAGES}, e.g. "{ME} at review".')
    task = (re.search(r'\bP\d+\b', why) or re.search('', '')).group(0) or None
    if task is None:
        sys.exit('Refused: name the task (P-number) the strike is for; strikes count per task.')
    todo = COMMS.goals.todos.get(task)
    if not todo.state.open or todo.assignment.owner != unit:
        sys.exit(f'Refused: {unit} does not hold {task}; strike the unit that holds it.')
    refuse(why, *OUTGOING)  # posted to #comms, so refused before Core records the strike
    call(CommsTodoTool(id=task, operation=StrikeTodo(
        todo.revision, COMMS.registry.require(ME).incarnation, todo.assignment, f'{why} (caught by {caught})')))
    n = struck(COMMS.goals.todos.get(task), unit)
    line = (f'{time.strftime("%Y-%m-%d %H:%M")} {unit} (strike {n} on {task}{f", {task} taken away" if n >= 2 else ""}; '
            f'caught by {caught}): {why}')
    on_record(line, 'Strike')
    print(f'{unit}: {n} strike(s) on {task}' + (f'. Reassign {task} now and freeze its branch.' if n >= 2 else ''))


def done(pid, sha, delivered=None):
    """Core checks the delivery on its clean checked commit; a late delivered commit is a strike."""
    todo = COMMS.goals.todos.get(pid)
    if not todo.state.open or not todo.pending:
        sys.exit(f'Refused: {pid} has no pending order')
    unit, repo = todo.assignment.owner, todo.order.check.directory
    operation = sha == 'op'  # an operation on live state, not a commit: the check runs now and now is delivery
    if not operation:
        diff = subprocess.run(['git', '-C', repo, 'show', '--first-parent', '-m', '--format=', sha], capture_output=True, text=True).stdout
        changed = re.findall(r'^\+\+\+ b/(\S+\.py)$', diff, re.M)
        after = [subprocess.run(['git', '-C', repo, 'show', f'{sha}:{f}'], capture_output=True, text=True).stdout for f in changed]
        if added := forwarders_added(diff, after):
            sys.exit(f'Refused: {sha} adds forwarders {added[:5]}; slop never counts as delivery. Order the hierarchy fix.')
    thread = COMMS.registry.require(unit)
    if left := [str(t) for t in COMMS.registry.storage_rule.checkouts_of(thread) if pid.lower() in t.name.lower()]:
        sys.exit(f'Refused: {unit} still has the checkout {left} for this order; storage rule: it removes it before the order counts as done.')
    try:
        COMMS.registry.storage_rule.require_quota(thread)
    except SelectedToolDenied as quota:
        sys.exit(f'Refused: {unit}: {quota}; storage rule: it cleans up before the order counts as done.')
    delivery = OperationDelivery() if operation else CommitDelivery(commit=sha, delivered_commit=delivered)
    call(CommsOrderDoneTool(problem=pid, delivery=delivery))
    lateness = COMMS.goals.todos.get(pid).order.delivery.at.lateness(todo.deadline)
    # An op has no commit time: recording it later is my delay, so lateness is judged at the OVERDUE alarm.
    if not operation and lateness.late():
        strike(unit, f'{pid} order {todo.order.id} {lateness} as {delivered}')
        return
    print(f'{unit}: order {todo.order.id} met as {delivered or sha}')


ACTIVITY = f'{RTS}/activity.log'


def first_calls():
    """Each order starts a first-tool-call clock: 2 minutes, separate from its commit deadline. Core records each mark."""
    lines = open(ACTIVITY).read().splitlines()[-4000:]
    watched = {line[9:].split(' ', 1)[0] for line in lines}
    missing = []
    for todo in held_orders():
        unit, order = todo.assignment.owner, todo.order
        if not todo.pending or unit not in watched:
            continue  # tool_watch cannot see this unit's harness; its clock is the commit deadline
        since = time.strftime('%H:%M:%S', time.localtime(order.at.at))
        acted = any(line[9:].startswith(unit + ' ') and line[:8] >= since for line in lines)
        if not acted and time.time() < order.first_call_deadline.at:
            continue
        event = ObservedFirstCallEvent if acted else NotifiedFirstCallEvent
        call(CommsTodoTool(id=todo.id, operation=RecordFirstCallTodo(
            todo.revision, COMMS.registry.require(ME).incarnation, event)))
        if not acted and COMMS.goals.todos.get(todo.id).revision != todo.revision:
            missing.append(f'{unit} (order {order.id}, sent {hhmm(order.at)})')
    return missing


HUNT_ROOTS = [(str(repo.checkout), repo.source) for repo in CONFIG.repos]


def hunt(where, why, sent=None):
    """A hot-path read with a verdict; HuntVerb declares its file:line, reason and outcome."""
    path, _, line = where.rpartition(':')
    if not os.path.exists(path) or not line.isdigit() or int(line) > sum(1 for _ in open(path)):
        sys.exit(f'Refused: {where} is not a real file:line')
    if len(why.split()) < 10:
        sys.exit('Refused: say what the code does per event or per history row, and the fix or why it is clean (10+ words)')
    if sent:
        held = held_orders()
        todo = next((t for t in held if t.order.id == sent), None) or sys.exit(
            f'Refused: {sent} is not an open tracked order; send it first. {HuntVerb.usage()}')
    else:
        named = set(re.findall(r'\bP\d+\b', why))
        held = held_orders()
        todo = next((t for t in held if t.id in named), None) or sys.exit(
            'Refused: a clean hunt is recorded on its open problem; name the P-number in --why. '
            + HuntVerb.usage())
    ref, at = FileLine(os.path.realpath(path), int(line)), RecordedEventTime(time.time())
    call(CommsTodoTool(id=todo.id, operation=RecordHuntTodo(
        todo.revision, COMMS.registry.require(ME).incarnation,
        OrderedHunt(ref, why, at, sent) if sent else CleanHunt(ref, why, at))))
    print(f'hunt recorded: {where} -> {sent or "clean"}')


def hunts():
    history = COMMS.goals.todos.history_for_commander(COMMS.registry.require(ME).incarnation)
    return [h for t in history for h in t.hunts]


def next_hunt():
    """Least-recently hunted file among the most-churned production files on origin/main."""
    hunted = {}
    for h in hunts():
        hunted[h.where.file] = max(hunted.get(h.where.file, 0), h.at.at)
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


def reply(unit: str, since: float) -> dict | None:
    """The unit's latest message to me after ``since``, or None."""
    if not os.path.exists(f'{B}/bus.jsonl'):
        return None
    with open(f'{B}/bus.jsonl', 'rb') as f:
        f.seek(start := max(0, os.path.getsize(f.name) - 3_000_000))
        lines = f.read().splitlines()[bool(start):]
    latest = None
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        to = str(m.get('to'))
        if m.get('from') == unit and float(m.get('ts', 0)) > since and (ME in to or '#comms' in to):
            latest = m
    return latest


def replied(latest: dict | None) -> str:
    """The reply an overdue alarm is read against."""
    if latest is None:
        return '; no reply since the order'
    at = time.strftime('%H:%M', time.localtime(float(latest['ts'])))
    return f'; REPLIED {at} [{latest["id"]}]: {" ".join(str(latest.get("text")).split())[:160]} -- read it before pushing'


def deadline_lines(pending: list[Todo], read: dict[str, OrderProgress], since: float = 0.0) -> list[str]:
    """Every past-due unrecorded order alarms with its state until recorded.
    Stall crossings remain separate from deadline alarms."""
    now = time.time()
    late, stalled = [], []
    for t in pending:
        ordered, work = t.order.at.recorded_at('An order'), read[t.id]
        if work.stalled_between(ordered, since, now):
            stalled.append(f'STALL {t.id} {t.holder}: {work.line} -> push it now or reassign')
        if not t.overdue(now):
            continue
        answer = reply(t.holder, ordered)
        due = f'due {hhmm(t.deadline)}, order {t.order.id}'
        late.append(f'{t.holder} ({due}, {work.line}{replied(answer)})')
    alarm = (f'OVERDUE: {", ".join(late)} -> record merged work; '
             'push or merge unfinished work now')
    return [alarm] * bool(late) + stalled


def install_lines(held: tuple[Todo, ...], read: dict[str, OrderProgress], now: float) -> list[str]:
    """One INSTALL line naming every held order that waits on the fleet's install."""
    waiting = [f'{w.id} {w.holder}' for t in held for w in t.order.waiting_on_install(t, read)]
    return InstallDeadline.read(COMMS.root).alarm(waiting, now)


def overdue():
    since = 0.0  # previous poll bounds stall crossings; deadline alarms repeat
    installs: list[str] = []
    while True:
        now = time.time()
        if missing := first_calls():
            print('NO FIRST CALL: ' + ', '.join(missing) + ' -> stuck holding an order; push it now or reassign',
                  flush=True)
        held = held_orders()
        stale = [f'{t.id} {t.assignment.owner} (last {seen})' for t in held
                 if (seen := last_activity(t.assignment.owner)) and seen < time.strftime('%H:%M:%S', time.localtime(time.time() - 1200))]
        if stale:
            print(f'IDLE OWNER 20+ min: {", ".join(stale)} -> push or reassign', flush=True)
        pending = [t for t in held if t.pending]
        read = progress() if pending else {}
        for line in deadline_lines(pending, read, since):
            print(line, flush=True)
        if (lines := install_lines(held, read, now)) != installs:
            for line in lines:
                print(line, flush=True)
            installs = lines
        # Units leave finished PRs open and wander off. An order's open PR that nobody has touched for
        # 5 minutes is a stall, whatever the unit says it is doing; until its deadline, OVERDUE owns the alarm.
        idle_prs = [f'{t.holder} {t.repo.split("/")[1]} {read[t.id].line} ({t.id})'
                    for t in pending if t.overdue(now) and read[t.id].untouched(now)]
        if idle_prs:
            print('STALL PR open and untouched 5+ min: ' + ', '.join(idle_prs) + ' -> order the merge or the fix now', flush=True)
        for _ in HuntDue.broken(COMMS, ME, since):
            print(HuntVerb.due(next_hunt()), flush=True)
        since = now
        time.sleep(60)


@dataclass(frozen=True, kw_only=True)
class CommanderVerb(DeclaredFamily, DeclaredOptions, affix="Verb"):
    """One commander verb: its options are its fields, its parser comes from them, and run() does the work."""
    fork: str | None = option("--fork", default=None, help="a forked commander's name; keeps its beat fresh")

    @property
    def who(self) -> str:
        return self.fork or ME

    def apply(self, ctx: None = None) -> None:
        self.gate()
        self.run()

    def gate(self) -> None:
        if self.fork:
            capacity.beat(self.fork)

    @abstractmethod
    def run(self) -> None: ...

    @classmethod
    def tool(cls) -> str:
        """The agent-comms-commander tool this verb belongs to: its module's name."""
        return cls.__module__.rpartition('.')[2].replace('_', '-')

    @classmethod
    def usage(cls) -> str:
        """This verb's command line, rendered by the parser its options declare."""
        verbs = argparse.ArgumentParser(prog=cls.tool()).add_subparsers()
        cls.add_parser(verbs)
        return ' '.join(next(iter(verbs.choices.values())).format_usage().split()[1:])

    @classmethod
    def parse(cls, argv: list[str], description: str) -> CommanderVerb:
        parser = argparse.ArgumentParser(prog=cls.tool(), description=description,
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        verbs = parser.add_subparsers(dest='command', required=True)
        for member in cls.members_with(cls):
            member.add_parser(verbs)
        args = parser.parse_args(argv)
        return cls.member_named(args.command).from_namespace(args)


class RuledVerb(CommanderVerb):
    """HARD RULE: no commander action, mine or a fork's, on rules not re-read in 20 min."""

    def gate(self) -> None:
        super().gate()
        for due in RulesDue.violations(COMMS, self.who):
            flag = '' if self.who == ME else f' --as {self.who}'
            sys.exit(f'REFUSED, nothing recorded: {due.why}. Run agent-comms-commander'
                     f' rules review{flag}, then answer each drawn rule{flag}.')


def unstrike(task: str, why: str, at: str | None = None) -> None:
    """A strike recorded in error is voided in Core, then narrated and posted to #comms."""
    todo = COMMS.goals.todos.get(task)
    live = [s for s in todo.live_strikes() if at in (None, hhmm(s.at))]
    if not live:
        sys.exit(f'Refused: {task} has no live strike' + (f' recorded at {at}.' if at else '.'))
    voided = live[-1]
    refuse(why, *OUTGOING)  # posted to #comms, so refused before Core records the void
    call(CommsTodoTool(id=task, operation=VoidStrikeTodo(
        todo.revision, COMMS.registry.require(ME).incarnation, voided, why)))
    unit = voided.previous.owner
    n = struck(COMMS.goals.todos.get(task), unit)
    line = (f'{time.strftime("%Y-%m-%d %H:%M")} {unit} (strike voided on {task}, '
            f'recorded {hhmm(voided.at)}; {n} strike(s) remain): {why}')
    on_record(line, 'Void')
    print(f'{unit}: {n} strike(s) on {task}')


def due(pid: str, at: str, why: str) -> None:
    """Core moves a held order's deadline, its assignment unchanged; the reason goes on record."""
    todo = COMMS.goals.todos.get(pid)
    if not todo.pending:
        sys.exit(f'Refused: {pid} holds no pending order whose deadline could move.')
    if len(why.split()) < 8:
        sys.exit('Refused: --why says why the deadline moves (8+ words)')
    refuse(why, *OUTGOING)  # posted to #comms, so refused before Core records the deadline
    moved = deadline(at)
    call(CommsTodoTool(id=pid, operation=AssignTodo(
        todo.revision, todo.assignment, moved, todo.order, todo.goal)))
    on_record(f'{time.strftime("%Y-%m-%d %H:%M")} {todo.holder} ({pid} order {todo.order.id} due '
              f'{hhmm(todo.deadline)} moved to {hhmm(moved)}): {why}', 'Deadline')
    print(f'{pid}: due {hhmm(moved)}')


def withdraw(pid: str, why: str, superseded_by: str | None = None) -> None:
    """Core retires an undelivered order sent in error.

    The reason goes on the public record and to the unit that held it."""
    todo = COMMS.goals.todos.get(pid)
    refuse(why, *OUTGOING)  # posted to #comms and the holder, so refused before Core records it
    call(CommsTodoTool(id=pid, operation=WithdrawTodo(
        todo.revision, COMMS.registry.require(ME).incarnation, todo.assignment, why,
        superseded_by)))
    withdrawal, = COMMS.goals.todos.get(pid).order.withdrawals(todo)
    on_record(f'{time.strftime("%Y-%m-%d %H:%M")} {withdrawal}', 'Withdrawal')
    post([todo.assignment.owner],
         f'Withdrawn: {withdrawal}. Stop that work; it counts against no one.')
    board()
    print(withdrawal)


def fire(unit: str, why: str) -> None:
    """Core releases every open problem the unit holds in one call; orders and strikes stay."""
    try:
        reason = StrikeReason(why)
    except ValueError as refused:
        sys.exit(f'Refused: {refused}')
    for todo in call(CommsFireTool(unit=unit, reason=reason)).todos:
        print(f'{todo.id}: released from {unit}: {why}')


class LoadedVerb(RuledVerb):
    """HARD RULE: overloaded means fork, not more orders from me; a fork is never blocked."""

    def gate(self) -> None:
        if not self.fork:
            capacity.Load.of(COMMS, ME).require()
        super().gate()


class OrderVerb(CommanderVerb):
    """A verb of `order`."""


@dataclass(frozen=True, kw_only=True)
class SendVerb(OrderVerb, LoadedVerb):
    help = "record the order in Core and send it"
    unit: str = option("unit")
    due: str = option("due", help="HH:MM local time")
    body_file: str = option("body_file", metavar="BODYFILE")
    clean: bool = option("--clean", default=False, help="the unit showed no incompetence")
    mechanical: bool = option("--mechanical", default=False)
    reassign: str | None = option("--reassign", default=None, help="the concrete failure that forces replacement")
    request: str = option("--request", help="the owner request this order serves")
    traced: str | None = option("--traced", default=None, metavar="PATH:LINE",
                                help="the first failing source line, required after two owners")

    def run(self) -> None:
        send(self.unit, self.due, self.body_file, self.request, clean=self.clean,
             mechanical=self.mechanical, reassign=self.reassign, traced=self.traced)


@dataclass(frozen=True, kw_only=True)
class StrikeVerb(OrderVerb, RuledVerb):
    help = "a send-back, a missed deadline or a false claim"
    unit: str = option("unit")
    why: str = option("--why")
    caught: str = option("--caught", default=f'{ME} at review', help='"WHO at STAGE"; stage is review/install/live/user')

    def run(self) -> None:
        strike(self.unit, self.why, self.caught)


@dataclass(frozen=True, kw_only=True)
class FireVerb(OrderVerb, RuledVerb):
    help = "take every open problem off a unit; their orders and strikes stay"
    unit: str = option("unit")
    why: str = option("--why")

    def run(self) -> None:
        fire(self.unit, self.why)


@dataclass(frozen=True, kw_only=True)
class UnstrikeVerb(OrderVerb, RuledVerb):
    help = "void a strike recorded in error; it stays on the record and stops counting"
    pid: str = option("pid")
    why: str = option("--why")
    at: str | None = option("--at", default=None,
                            help="HH:MM the strike was recorded; default the newest")

    def run(self) -> None:
        unstrike(self.pid, self.why, self.at)


@dataclass(frozen=True, kw_only=True)
class DueVerb(OrderVerb, RuledVerb):
    help = "move a held order's deadline; the reason goes on the public record"
    pid: str = option("pid")
    at: str = option("at", help="HH:MM the order is now due")
    why: str = option("--why")

    def run(self) -> None:
        due(self.pid, self.at, self.why)


@dataclass(frozen=True, kw_only=True)
class InstallVerb(RuledVerb):
    help = "plan the fleet's next install; merged and delivered orders wait on it"
    at: str = option("at", help="HH:MM the install is due")

    def run(self) -> None:
        PlannedInstallDeadline(deadline(self.at)).plan(COMMS.root)
        print(f'install due {self.at}')


@dataclass(frozen=True, kw_only=True)
class WithdrawVerb(OrderVerb, RuledVerb):
    help = "retire an undelivered order sent in error; its problem closes with no strike"
    pid: str = option("pid")
    why: str = option("--why")
    superseded_by: str | None = option("--superseded-by", default=None, metavar="PID",
                                       help="the problem that already owns this fix")

    def run(self) -> None:
        withdraw(self.pid, self.why, self.superseded_by)


@dataclass(frozen=True, kw_only=True)
class DoneVerb(OrderVerb, RuledVerb):
    help = "Core checks the delivered commit; late delivery is a strike"
    pid: str = option("pid")
    sha: str = option("--sha", help="the checked commit (the check runs on it) or op")
    delivered: str | None = option("--delivered", default=None, metavar="SHA",
                                   help="the unit's pushed ancestor of --sha; lateness reads its time")

    def run(self) -> None:
        done(self.pid, self.sha, self.delivered)


@dataclass(frozen=True, kw_only=True)
class PullRequestVerb(OrderVerb, declared_name="pr"):
    help = "open the problem's PR with its required immutable order identity"
    pid: str = option("pid")
    title: str = option("--title")
    body_file: str = option("--body-file")

    def run(self) -> None:
        todo = COMMS.goals.todos.get(self.pid)
        checkout = COMMS.registry.storage_rule.order_checkout(todo.holder, todo.id)
        print(todo.order.open_pull_request(
            todo, checkout, self.title, Path(self.body_file).read_text()))


@dataclass(frozen=True, kw_only=True)
class CheckVerb(OrderVerb, RuledVerb):
    help = "replace an open order's wrong check with one passing on main; keeps deadline, strikes"
    pid: str = option("pid")
    check: str = option("--check", help='"<repo> :: <shell command> -> <integer>"')
    why: str = option("--why", help="what was wrong with the recorded check")

    def run(self) -> None:
        correct_check(self.pid, self.check, self.why)


@dataclass(frozen=True, kw_only=True)
class ServeVerb(OrderVerb, RuledVerb):
    help = "name the plan phase an open order serves; keeps its id, deadline and strikes"
    pid: str = option("pid")
    phase: str = option("phase", help="one of " + ", ".join(OrderPhase.names()))

    def run(self) -> None:
        call(CommsOrderServeTool(problem=self.pid, serves=OrderPhase.member_named(self.phase)))
        print(f'{self.pid} serves {self.phase}')


@dataclass(frozen=True, kw_only=True)
class SayVerb(OrderVerb, LoadedVerb):
    help = "a non-order message; refused if it orders work"
    units: str = option("units", metavar="UNIT[,UNIT]")
    body_file: str = option("body_file", metavar="BODYFILE")

    def run(self) -> None:
        say(self.units, self.body_file)


@dataclass(frozen=True, kw_only=True)
class ProblemVerb(OrderVerb, LoadedVerb):
    help = "Core records the problem with its order, or nothing"
    text: str = option("text")
    to: str = option("--to")
    due: str = option("--due", help="HH:MM local time")
    order: str = option("--order", help="the order body file")
    request: str = option("--request", help="the owner request this order serves")

    def run(self) -> None:
        problem(self.text, self.to, self.due, self.order, self.request)


@dataclass(frozen=True, kw_only=True)
class CloseVerb(OrderVerb, RuledVerb):
    help = "the fix works in the app; Core closes the problem"
    pid: str = option("pid")
    works: str = option("--works")
    artifact: str = option("--artifact")

    def run(self) -> None:
        close(self.pid, self.works, self.artifact)


@dataclass(frozen=True, kw_only=True)
class HuntVerb(OrderVerb, LoadedVerb):
    help = "a hot-path read with a verdict: file:line, what scales or repeats, and the order it became"
    where: str = option("where", metavar="FILE:LINE")
    why: str = option("--why")
    sent: str | None = option("--sent", default=None, group="outcome", metavar="ORDER",
                              help="the open order the hunt became")
    clean: bool = option("--clean", default=False, group="outcome",
                         help="no order; --why names the open P-number it is recorded on")

    @classmethod
    def due(cls, where: str | None) -> str:
        return (f'HUNT DUE (none in 15 min): read {where} for per-event or per-history work '
                f'or slop, then {cls.usage()}')

    def run(self) -> None:
        hunt(self.where, self.why, self.sent)


class NextHuntVerb(OrderVerb, declared_name="next-hunt"):
    help = "print the next place to hunt"

    def run(self) -> None:
        print(next_hunt())


class BoardVerb(OrderVerb):
    help = "regenerate fleet-board.md from Core's Todo store"

    def run(self) -> None:
        board()


class OverdueVerb(OrderVerb):
    help = "loop: every minute, name units past their deadline"

    def run(self) -> None:
        overdue()


def main(a):
    OrderVerb.parse(a, __doc__).apply()
