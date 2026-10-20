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
from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import time
from abc import abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from pydantic import PrivateAttr

from agent_comms_hooks import ClaudeHook
from agent_comms_hooks.watch import PeerSync

from ..change_report import FileLine
from ..child_process import BoundedRun, CheckPrinted
from ..cli_commands import DeclaredOptions, option
from ..comms import Comms
from ..declared_family import DeclaredFamily
from ..external_transcript import ExternalTranscript
from ..field_codec import boundary_decoder
from ..goal_actions import CreateProblemGoalAction, OrderGoalAction
from ..goals import OrderCheck, ProblemGoalOrder
from ..native_tool_call import SelectedToolDenied
from ..plan_phases import OrderPhase, UnplannedPhase
from ..progress_claims import MergedClaim, WorkingClaim
from ..recorded_time import RecordedEventTime
from ..repo import Repo
from ..standing_rules import HuntDue, RulesDue
from ..storage_rules import UnitStorage
from ..thread_identity import ThreadIncarnation
from ..todos import (
    ApplicationOrderRun,
    AssignTodo,
    BluntText,
    CleanHunt,
    CommitDelivery,
    DirectiveText,
    FileDeliveryEvidence,
    InstallDeadline,
    LocatedProblemFinding,
    MechanicalOrderRun,
    NoDeliveryEvidence,
    NotifiedFirstCallEvent,
    ObservedFirstCallEvent,
    OperationDelivery,
    OrderDeletion,
    OrderedHunt,
    OrderMessage,
    OrderProgress,
    OrderRun,
    PendingProblemOrder,
    PlannedInstallDeadline,
    RecordFirstCallTodo,
    Slop,
    StrikeReason,
    StrikeTodo,
    Todo,
    UnlocatedProblemFinding,
    VerdictAudience,
    VoidStrikeTodo,
    WithdrawTodo,
)
from ..tool_caller import ThreadCaller
from ..tools import (
    CommsChangeProblemTool,
    CommsFireTool,
    CommsOrderCheckTool,
    CommsOrderDoneTool,
    CommsOrderServeTool,
    CommsOrderTool,
    CommsProblemCloseTool,
    CommsSendTool,
    CommsTodoCreateTool,
    CommsTodoListTool,
    TodoList,
)
from . import capacity
from .config import CommanderConfig, CommanderRole
from .send_guard import CommanderSession

OUTGOING = DirectiveText.OUTGOING  # every commander text passes each one

def refuse(text, *families):
    """Core's text families decide what the commander may write; nothing is recorded when one refuses."""
    if refused := [r for family in families if (r := family.refused(text))]:
        sys.exit('REFUSED, nothing recorded: ' + ' '.join(refused))


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


STAGES = ('tool call', 'review', 'install', 'live', 'user')  # where a mistake was caught; later is worse


def revision(repo: str, ref: str) -> str:
    """Resolve a supplied Git revision once at the command boundary."""
    return subprocess.check_output(
        ['git', '-C', repo, 'rev-parse', '--verify', f'{ref}^{{commit}}'], text=True).strip()


class CommanderContext(CommanderConfig):
    """The selected root's bus and canonical reads, shared by command families."""

    _comms: Comms = PrivateAttr()

    def model_post_init(self, context: object) -> None:
        self._comms = Comms(self.root)

    @property
    def comms(self) -> Comms:
        return self._comms

    @property
    def incarnation(self) -> ThreadIncarnation:
        return self.comms.registry.require(self.commander).incarnation

    def post(self, audience: VerdictAudience, body: str) -> str:
        target = audience.threads[0] if len(audience.threads) == 1 else '#comms'
        published = self.call(CommsSendTool(sender=self.commander, target=target, body=body,
                                          audience=audience.threads))
        PeerSync.sent(self, target)
        return published.id

    def call(self, tool):
        return ThreadCaller(self.commander).invoke(tool, self.comms)

    def todos(self):
        store = self.comms.goals.todos
        store.reconcile_merges(self.incarnation, self.repos, self.comms.registry.storage_rule)
        return store.for_commander(self.incarnation)

    def read_check(self, line: str) -> OrderCheck:
        if not (check := OrderCheck.parse(line)):
            sys.exit('Refused: Check reads "<repo> :: <shell command> -> <integer>"')
        directory = self.repo_directory(check.directory)
        if not directory.is_dir():
            sys.exit(f'Refused: Check repo {check.directory} is not a directory')
        return replace(check, directory=str(directory))

    def held_orders(self) -> tuple[Todo, ...]:
        return self.comms.goals.todos.held_for_commander(self.incarnation)

    def held_by(self, unit: str) -> tuple[Todo, ...]:
        return self.comms.goals.todos.held_by(self.incarnation, unit)

    def units(self) -> frozenset[str]:
        return self.comms.goals.todos.units(self.incarnation)

    def progress(self) -> dict[str, OrderProgress]:
        listed: TodoList = self.call(CommsTodoListTool())
        return listed.progress

    def importers(self, modules: set[str], check: OrderCheck) -> list[str]:
        """The other configured repos whose `from agent_comms` imports name a module, unless the
        check reads them."""
        pattern = rf'^\s*(from|import) agent_comms\b.*\b({"|".join(sorted(modules))})\b'
        return [str(repo.checkout) for repo in self.repos
                if repo.checkout != Path(check.directory).resolve()
                and str(repo.checkout) not in check.command
                and subprocess.run(['grep', '-rqE', '--include=*.py', pattern,
                                    str(repo.checkout / repo.source)]).returncode == 0]

    def last_activity(self, unit):
        return ExternalTranscript(self.comms.root,
            self.comms.registry.require(unit).incarnation).last_tool_call()

    def hunts(self):
        return self.comms.goals.todos.hunts_for_commander(self.incarnation)

    def reply(self, unit: str, since: float) -> str:
        """The newest reply suffix for this command's overdue alarm."""
        with self.comms.bus.log.opened_wire_snapshot() as source:
            for message in source.newest_messages():
                if (message.sender == unit and message.timestamp > since
                        and message.target in (self.commander, '#comms')):
                    at = time.strftime('%H:%M', time.localtime(message.timestamp))
                    return (f'; REPLIED {at} [{message.message_id}]: '
                            f'{" ".join(message.body.split())[:160]} -- read it before pushing')
        return '; no reply since the order'


class Commander(CommanderContext):
    @boundary_decoder
    def send(self, unit, due, body: str, clean=False, mechanical=False, reassign=None, new=None,
             traced=None, replaces=None, disk=False, finding=UnlocatedProblemFinding()):
        """Validate an order body and record it in Core: a new problem (new=(PID, TEXT)) or the listed open problems.

        Sending to a unit that already holds the problem replaces its order there; without --clean that is a send-back."""
        problems = []
        # Below the floor Core's StorageRule denies each allocating tool call, so the order prose is not checked.
        try:
            self.comms.registry.storage_rule.require_quota(self.comms.registry.require(unit))
        except SelectedToolDenied as quota:
            problems.append(f'{unit}: {quota}: it cleans up its own checkouts and scratch before it gets new work')
        Todo.require_order_body(body)
        if not clean and (timid := BluntText.refused(body)):  # blunt at all times unless nothing was incompetent
            problems.append(timid)
        works = re.search(rf'(?m)^{re.escape(OrderRun.label)}\s*(.+)$', body)
        run = MechanicalOrderRun() if mechanical else ApplicationOrderRun(works[1] if works else '')
        serves = re.search(r'(?m)^Serves:\s*(\S+)\s*$', body)
        phase = OrderPhase.member_named(serves[1] if serves else UnplannedPhase.declared_name)
        edits = tuple(re.findall(r'(?m)^\s*\d+[.)] (.+?)\s*$', body))
        if not edits:
            problems.append('no numbered edits: list each exact edit as "1. ...", "2. ..."')
        report = re.search(r'(?m)^Report:\s*(\S.*?)\s*$', body)
        if not report:
            problems.append('no Report: line: say exactly what the unit sends back (sha, counts before -> after)')
        banned = tuple(item.strip() for line in re.findall(r'(?m)^Banned:(.*)$',
            body) for item in line.split(';') if item.strip())
        if not banned:
            problems.append('no Banned: line: name the slop shapes this task invites, separated by ";"')
        if not re.fullmatch(r'\d{1,2}:\d{2}', due):
            problems.append('due must be HH:MM local time')
        fact = re.search(r'(?m)^Problems?:\s*(P\d+(?:\s*,\s*P\d+)*)\b', body)
        group = [new[0]] if new else re.findall(r'P\d+', fact[1]) if fact else []
        owner = re.search(r'(?m)^Owner:\s*(\S+):(\d+)\s*$', body)
        line = re.search(r'(?m)^Check:(.*)$', body)
        check = self.read_check(line[1] if line else '')
        listed = {t.id: t for t in self.todos()}
        if not group:
            problems.append('no Problem: P<n> line; record it first with `order problem` (every order owns a listed problem)')
        elif not new and (missing := [pid for pid in group if pid not in listed]):
            problems.append(f'{missing} not open problems')
        if not owner:
            problems.append('no Owner: path:line of the existing owner the fix belongs on')
        elif not os.path.exists(owner[1]) or int(owner[2]) > sum(1 for _ in open(owner[1])):
            problems.append(f'Owner {owner[1]}:{owner[2]} does not exist; read the code and name the real owner')
        # A check that can never print its target, or a premise naming lines that do something else, wastes the order.
        # An order's check must fail now (or it proves nothing), and every absolute
        # path:line it cites must exist.
        if check and os.path.isdir(check.directory) and not problems:
            try:
                ran = subprocess.run(['bash', '-c', check.command],
                    cwd=check.directory, capture_output=True, timeout=90)
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
        if check and deleted and (unread := self.importers(deleted, check)):
            problems.append(f'the order deletes {sorted(deleted)}, which {unread} import; '
                            'the check must also count their `from agent_comms` imports')
        if any(re.search(r'(?<![\w/])tools/', edit) for edit in edits) and not all(
                said in body for said in ('--root tools', '--root src')):
            problems.append('the order changes tools/: say "ratchet with --root tools and with '
                            '--root src, each as its own command"')
        # Ownership reversals waste work: one owner through completion; a change of owner names the concrete failure.
        holders = sorted({listed[pid].holder for pid in group if pid in listed} - {unit, None})
        replacing = not new and bool(group) and all(pid in listed and listed[pid].holder == unit for pid in group)
        for pid in group:
            if replacing:
                listed[pid].require_replacement(replaces)
        if replaces and not replacing:
            raise ValueError('--replaces must name the current order on these problems to the same unit.')
        trace = FileLine.from_wire(traced) if traced is not None else None
        for pid in group:
            if pid in listed and listed[pid].assignment:
                listed[pid].assignment.require_trace(unit, trace)
        if holders and not reassign:
            problems.append(f'fact already owned by {holders}; keep that owner, or pass --reassign "<the concrete failure: provider diagnostic, strikes, missed deadline>"')
        if reassign is not None and len(reassign.split()) < 8:
            problems.append('--reassign needs the concrete failure that forces replacement (8+ words), told to the old and new owner')
        if problems:
            sys.exit('Refused order:\n  ' + '\n  '.join(problems))
        message = (f'{body.rstrip()}' + ('\nThis replaces my earlier order on it; drop that one.' if replacing else '') + (f'\nTaken over because: {reassign}' if reassign else '') + '\n' + self.comms.registry.storage_rule.order_text(unit, [f'{repo.checkout}/.venv/bin/python' for repo in self.repos]) + '\nIf two lines of this order conflict, take the narrower reading, say which one you took, and proceed; do not stop to ask.'
                   + '\nStart now and push as you go.')  # a unit shown a due time takes until then
        taken = {other: f'Taken off you: {", ".join(group)} ("{listed[group[0]].text[:120]}") now belongs to {unit} because: {reassign}. Stop that work; do not touch its files.' for other in holders}
        refuse('\n'.join((message, *taken.values())),
            *OUTGOING)  # before Core: refused records nothing
        fired = [pid for pid in group if pid in listed and listed[pid].struck_out(unit)]
        if not mechanical and fired:
            sys.exit(f'Refused: {unit} has {ProblemGoalOrder.StrikeLimit} strikes on {fired}. '
                     f'Reassign them; only --mechanical orders on them reach {unit}.')
        # A miss already struck on the order this replaces is one miss:
        # the send-back records no second strike.
        struck_already = replacing and listed[group[0]].struck_on_order(unit)
        at = time.time()
        order = PendingProblemOrder(finding=finding,
            id=uuid4().hex[:12], owner=FileLine(owner[1], int(owner[2])),
            check=check, edits=edits, banned=banned,
            report=report[1], run=run, serves=phase,
            deletes=OrderDeletion.parse(deletes[1] if (deletes := re.search(
                rf'(?m)^{re.escape(OrderDeletion.label)}\s*(.+)$', body)) else None),
            at=RecordedEventTime(at), first_call_deadline=RecordedEventTime(at + 120))
        common = dict(order=order, request=order.request(self.comms.registry.require(self.commander), tuple(group)), deadline=deadline(due), commander=self.commander)
        if new:
            self.call(CommsTodoCreateTool(owner=unit, order=CreateProblemGoalAction(
                problem=new[0], repo=Repo.of(Path(check.directory)).github, text=new[1], **common)))
        for pid in [] if new else group:
            self.call(CommsOrderTool(owner=unit, order=OrderGoalAction(
                problem=pid, expected_revision=self.comms.goals.todos.get(pid).revision,
                assigner=self.incarnation, traced=trace, **common)))
        message += "\n" + "\n".join(order.pull_request_instruction(pid) for pid in group)
        sent = self.post(VerdictAudience((unit,)), message)
        # The message is each order's recorded link; a verdict citing it lands on that order.
        for pid in group:
            self.call(CommsChangeProblemTool(problem=pid, operation=OrderMessage(
                self.comms.goals.todos.get(pid).revision, self.incarnation, sent)))
        for other, text in taken.items():
            self.post(VerdictAudience((other,)), text)
        print(order.id)
        if replacing and not clean and struck_already:
            print(f'{unit} already struck on this order of {group[0]}; '
                  'the send-back counts that one miss once')
        elif replacing and not clean and not reassign:
            # A send-back is a strike, no warnings, and every send-back is roasted in #comms.
            # Done here so the commander cannot send work back and forget either.
            verdict = next(l for l in body.splitlines() if BluntText.found(l)).strip()
            self.strike(unit, group[0], f'sent back on {group[0]}: {verdict}')

    def correct_check(self, pid: str, line: str, why: str) -> None:
        """Core replaces a wrong check of an open order. The new check must already pass on main, so a
        correction fixes the commander's check and never passes undelivered work: done still runs it on
        the --sha it is given."""
        todo = self.comms.goals.todos.get(pid)
        if not todo.state.open or not todo.pending:
            sys.exit(f'Refused: {pid} has no pending order')
        check = self.read_check(line)
        if len(why.split()) < 8:
            sys.exit('Refused: --why says what was wrong with the recorded check (8+ words)')
        def git(*args: str) -> str:
            return subprocess.run(['git', '-C', check.directory, *args],
                                  capture_output=True, text=True).stdout.strip()
        head = git('rev-parse', 'HEAD')
        if head != git('rev-parse', 'origin/main') or git('status', '--porcelain', '-uno'):
            sys.exit(f'Refused: {check.directory} is not a clean checkout of origin/main; '
                     'the corrected check runs on main')
        check.result.require(asyncio.run(BoundedRun.run(
            ('bash', '-c', check.command), cwd=check.directory, timeout=90)))
        self.call(CommsOrderCheckTool(problem=pid, check=check, why=why))
        print(f'{pid}: order {todo.order.id} now checks `{check.command}`')

    def say(self, units, body_file):
        body = open(body_file).read()
        # Numbered edits or a report line are an order: it needs a deadline, so it goes through send.
        if len(re.findall(r'(?m)^\s*\d+[.)] ',
            body)) >= 2 or re.search(r'(?mi)^\s*(report|due)\b.*:', body):
            sys.exit('Refused: a numbered edit list or a Report/Due line is an order. Use `order send` so it gets a tracked deadline.')
        refuse(body, *OUTGOING)
        Slop.from_order(body)
        print(self.post(VerdictAudience(tuple(units.split(','))), body.rstrip()))

    def problem(self, text, unit, due, order_file, where, disk=False):
        """A problem is born owned: Core records it with its order in one step, or not at all."""
        location = FileLine.from_wire(where)
        location.require_exists()
        body = open(order_file).read()
        if 'Problem: NEW' not in body:
            sys.exit('Refused: the order file needs a "Problem: NEW" line; a problem is never logged without its order')
        if len(text.split()) < 8:
            sys.exit('Refused: describe the problem (8+ words): what is wrong, what it costs')
        history = self.comms.goals.todos.history_for_commander(self.incarnation)
        pid = f'P{max([int(t.id[1:]) for t in history if t.id[1:].isdigit()] + [0]) + 1}'
        self.send(unit, due, body.replace('Problem: NEW', f'Problem: {pid}'), new=(pid, text),
             disk=disk, finding=LocatedProblemFinding(location))
        self.write_board()
        print(pid)

    def close(self, pid, works, artifact=''):
        """Core owns closing: the installed evidence, the artifact and the no-open-order rule live on the Todo."""
        evidence = artifact or self.comms.goals.todos.get(pid).order.closing_artifact()
        ThreadCaller(self.commander).invoke(CommsProblemCloseTool(problem=pid,
            works=works, artifact=evidence), self.comms)
        self.write_board()
        print(f'{pid} closed')

    def write_board(self):
        lines = ['# Fleet board', '',
                 "GENERATED by agent-comms-commander order from Core's Todo store. Do not edit by hand:",
                 '`order problem` adds a row, `order send ... Problem: P<n>` orders it, `order close` or `order withdraw` removes it.', '',
                 '| Problem | Owner file | Owner | Order | Due | Progress | Delivered '
                 '| Strikes | Owner last acted |',
                 '|---|---|---|---|---|---|---|---|---|']
        listed, read = self.todos(), self.progress()
        activity = {unit: self.last_activity(unit) for unit in {t.holder for t in listed if t.holder}}
        for t in sorted(listed, key=lambda t: int(t.id[1:]) if t.id[1:].isdigit() else 0):
            unit, last = t.holder, t.released[-1:]
            owner = unit or f'unassigned: released from {last[0].previous.owner}: {last[0].reason.text}'
            lines.append(f'| {t.id}: {t.text} | {t.order.owner.to_wire()} | {owner} | {t.order.id} | '
                         f'{hhmm(t.deadline)} | {read[t.id].line if t.id in read else "-"} | '
                         f'{"no" if t.pending else "yes, install pending"} | '
                         f'{t.strike_count} | {activity[unit].clock() if unit else "-"} |')
        history = self.comms.goals.todos.history_for_commander(self.incarnation)
        lines += ['', '## Withdrawn orders', '',
                  *(f'- {line}' for t in history for line in t.order.withdrawals(t))]
        open(self.board, 'w').write('\n'.join(lines) + '\n')

    def on_record(self, line: str, recorded: str, audience: VerdictAudience) -> None:
        """Put one line on the public record: the narrative file, then its audience.

        Core already holds what `recorded` names, so a failed post is reposted, never recorded again.
        """
        new_file = not os.path.exists(self.narrative)
        with open(self.narrative, 'a') as f:
            if new_file:
                f.write('# What the fleet actually did\n\nEvery failure, in plain words, as it happened. '
                        'Written by agent-comms-commander order; never edited.\n\n')
            f.write(f'- {line}\n')
        try:
            self.post(audience, f'On the record: {line}')
        except (SystemExit, ValueError) as failed:
            print(f'{recorded} recorded, but the post failed ({failed}). '
                  'Repost with order.post; do not record it again.', file=sys.stderr)
            raise

    def strike(self, unit: str, task: str, why: str, caught: str = 'review', *,
               kind: type[StrikeTodo] = StrikeTodo) -> None:
        """HARD RULE: every failure goes on the public record, in plain words, damning and exact, so no
        failure stays private and none is described in words that hide what happened."""
        caught = f'{self.commander} at {caught}' if caught in STAGES else caught
        # What matters is how a mistake happened, how often, and how and when it was caught.
        if not any(stage in caught for stage in STAGES):
            sys.exit(f'Refused: --caught must say who caught it and at which stage {STAGES}, e.g. "{self.commander} at review".')
        todo = self.comms.goals.todos.get(task)
        todo.state.require_open(todo)
        if todo.held().owner != unit:
            sys.exit(f'Refused: {unit} does not hold {task}; strike the unit that holds it.')
        self.recorded(unit, todo, kind(todo.revision, self.incarnation, todo.held(),
                                      f'{why} (caught by {caught})'))

    def recorded(self, unit: str, todo: Todo, strike: StrikeTodo) -> None:
        """Core records the strike on the task's Todo; it is written to the narrative file and posted
        to #comms in the same step."""
        refuse(strike.reason,
            *OUTGOING)  # posted to #comms, so refused before Core records the strike
        if (self.call(CommsChangeProblemTool(problem=todo.id, operation=strike))
                .todo.revision == todo.revision):
            return
        self.posted(unit, self.comms.goals.todos.get(todo.id), strike)

    def posted(self, unit: str, todo: Todo, strike: StrikeTodo) -> None:
        """A strike Core recorded on the task's Todo goes to the narrative file and #comms."""
        n = struck(todo, unit)
        line = (f'{time.strftime("%Y-%m-%d %H:%M")} {unit} (strike {n} on {todo.id}'
                f'{f", {todo.id} taken away" if n >= 2 else ""}): {strike.reason}')
        self.on_record(line, 'Strike', VerdictAudience.of(unit, todo.goal))
        print(f'{unit}: {n} strike(s) on {todo.id}' + (f'. Reassign {todo.id} now and freeze its branch.' if n >= 2 else ''))

    def done(self, pid, sha, screenshot='', seen='', repo=''):
        """Core runs the delivery's check at its checked commit and strikes a late delivered commit
        in the same revision; this posts the strikes Core recorded."""
        todo = self.comms.goals.todos.get(pid)
        if not todo.state.open or not todo.pending:
            sys.exit(f'Refused: {pid} has no pending order')
        unit = todo.assignment.owner
        if repo and Path(repo).resolve() != Path(todo.order.check.directory).resolve():
            sys.exit('Refused: --repo must name the recorded order check directory.')
        repo = todo.order.check.directory
        operation = sha == 'op'  # an operation on live state, not a commit: the check runs now and now is delivery
        if not operation:
            diff = subprocess.run(['git', '-C', repo, 'show', '--first-parent',
                '-m', '--format=', sha], capture_output=True, text=True).stdout
            changed = re.findall(r'^\+\+\+ b/(\S+\.py)$', diff, re.M)
            after = [subprocess.run(['git', '-C', repo, 'show', f'{sha}:{f}'],
                capture_output=True, text=True).stdout for f in changed]
            if added := forwarders_added(diff, after):
                sys.exit(f'Refused: {sha} adds forwarders {added[:5]}; slop never counts as delivery. Order the hierarchy fix.')
        thread = self.comms.registry.require(unit)
        storage = UnitStorage.read(self.comms.registry.storage_rule, thread)
        checkout = storage.rule.order_checkout(unit, pid)
        if checkout in storage.extra_checkouts:
            sys.exit(f'Refused: {unit} still has the checkout {checkout} for this order; '
                     'storage rule: it removes it before the order counts as done.')
        try:
            storage.require_quota()
        except SelectedToolDenied as quota:
            sys.exit(f'Refused: {unit}: {quota}; storage rule: it cleans up before the order counts as done.')
        todo.order.run.require_artifact(seen or screenshot)
        evidence = FileDeliveryEvidence(seen or screenshot) if seen or screenshot else NoDeliveryEvidence()
        if operation:
            delivery = OperationDelivery(evidence=evidence)
            claim = WorkingClaim(evidence.artifact())
        else:
            delivered_commit = revision(repo, sha)
            if subprocess.run(['git', '-C', repo, 'merge-base', '--is-ancestor',
                               delivered_commit, 'HEAD']).returncode:
                sys.exit(f'Refused: {delivered_commit} is not in {repo} HEAD.')
            delivery = CommitDelivery(commit=revision(repo, 'HEAD'),
                                      delivered_commit=delivered_commit, evidence=evidence)
            claim = MergedClaim(delivery.delivered)
        self.call(CommsOrderDoneTool(problem=pid,
            order_id=todo.order.id, delivery=delivery, claims=(claim,)))
        delivered_todo = self.comms.goals.todos.get(pid)
        for late in delivered_todo.strikes[len(todo.strikes):]:
            self.posted(unit, delivered_todo, late)
        print(f'{unit}: order {todo.order.id} delivered as {delivery.delivered if not operation else sha}')

    def first_calls(self, held, activity):
        missing = []
        for todo in held:
            unit, order = todo.assignment.owner, todo.order
            if not todo.pending:
                continue
            acted = activity[unit].since(order.at.recorded_at("An order"))
            if not acted and time.time() < order.first_call_deadline.at:
                continue
            event = ObservedFirstCallEvent if acted else NotifiedFirstCallEvent
            self.call(CommsChangeProblemTool(problem=todo.id, operation=RecordFirstCallTodo(
                todo.revision, self.incarnation, event)))
            if not acted and self.comms.goals.todos.get(todo.id).revision != todo.revision:
                missing.append(f'{unit} (order {order.id}, sent {hhmm(order.at)})')
        return missing

    def hunt(self, where, why, sent):
        """Record the commander's source read, whether or not it became an order."""
        path, _, line = where.rpartition(':')
        if not os.path.exists(path) or not line.isdigit() or int(line) > sum(1 for _ in open(path)):
            sys.exit(f'Refused: {where} is not a real file:line')
        if len(why.split()) < 10:
            sys.exit('Refused: say what the code does per event or per history row, and the fix or why it is clean (10+ words)')
        ref, at = FileLine(os.path.realpath(path), int(line)), RecordedEventTime(time.time())
        self.comms.goals.todos.record_hunt(self.incarnation,
            OrderedHunt(ref, why, at, sent) if sent else CleanHunt(ref, why, at))
        print(f'hunt recorded: {where} -> {sent or "clean"}')

    def next_hunt(self):
        """Least-recently hunted file among the most-churned production files on origin/main."""
        hunted = {}
        for h in self.hunts():
            hunted[h.where.file] = max(hunted.get(h.where.file, 0), h.at.at)
        candidates = []
        for repo, root in [(str(repo.checkout), repo.source) for repo in self.repos]:
            log = subprocess.run(['git', '-C', repo, 'log', 'origin/main',
                '--since=7.days', '--name-only', '--format=', '--', root],
                                 capture_output=True, text=True).stdout.split()
            churn = {}
            for f in log:
                full = os.path.realpath(os.path.join(repo, f))
                if os.path.exists(full) and f.endswith('.py'):
                    churn[full] = churn.get(full, 0) + 1
            candidates += sorted(churn, key=churn.get, reverse=True)[:15]
        return min(candidates, key=lambda f: hunted.get(f, 0)) if candidates else None

    def deadline_lines(self, pending: list[Todo], read: dict[str, OrderProgress],
                       since: float = 0.0) -> list[str]:
        """Past-due unfinished orders alarm; installed merges have already closed.
        Core records a stalled order's strike and release; this view reports deadlines."""
        now = time.time()
        late = []
        for t in pending:
            ordered, work = t.order.at.recorded_at('An order'), read[t.id]
            if not t.overdue(now):
                continue
            answer = self.reply(t.holder, ordered)
            due = f'due {hhmm(t.deadline)}, order {t.order.id}'
            late.append(f'{t.holder} ({due}, {work.line}{answer})')
        alarm = f'OVERDUE: {", ".join(late)} -> push or merge unfinished work now'
        return [alarm] * bool(late)

    def install_lines(self, held: tuple[Todo, ...], read: dict[str, OrderProgress], now: float) -> list[str]:
        """One INSTALL line naming every held order that waits on the fleet's install."""
        waiting = [f'{w.id} {w.holder}' for t in held for w in t.order.waiting_on_install(t, read)]
        return InstallDeadline.read(self.comms.root).alarm(waiting, now)

    def overdue(self):
        since = 0.0  # previous poll bounds stall crossings; deadline alarms repeat
        installs: list[str] = []
        while True:
            now = time.time()
            held = self.held_orders()
            activity = {unit: self.last_activity(unit) for unit in {t.assignment.owner for t in held}}
            if missing := self.first_calls(held, activity):
                print('NO FIRST CALL: ' + ', '.join(missing) + ' -> stuck holding an order; push it now or reassign',
                      flush=True)
            stale = [f'{t.id} {t.assignment.owner} (last {activity[t.assignment.owner].clock()})' for t in held
                     if activity[t.assignment.owner].before(now - 1200)]
            if stale:
                print(f'IDLE OWNER 20+ min: {", ".join(stale)} -> push or reassign', flush=True)
            pending = [t for t in held if t.pending]
            read = self.progress() if pending else {}
            for line in self.deadline_lines(pending, read, since):
                print(line, flush=True)
            if (lines := self.install_lines(held, read, now)) != installs:
                for line in lines:
                    print(line, flush=True)
                installs = lines
            for _ in HuntDue.broken(self.comms, self.commander, since):
                print(HuntVerb.due(self.next_hunt()), flush=True)
            since = now
            time.sleep(60)

    @boundary_decoder
    def unstrike(self, task: str, why: str, at: str | None = None) -> None:
        """A strike recorded in error is voided in Core, then narrated and posted to #comms."""
        todo = self.comms.goals.todos.get(task)
        live = [s for s in todo.live_strikes() if at in (None, hhmm(s.at))]
        if not live:
            sys.exit(f'Refused: {task} has no live strike' + (f' recorded at {at}.' if at else '.'))
        voided = live[-1]
        refuse(why, *OUTGOING)  # posted to #comms, so refused before Core records the void
        self.call(CommsChangeProblemTool(problem=task, operation=VoidStrikeTodo(
            todo.revision, self.incarnation, voided, why)))
        unit = voided.previous.owner
        n = struck(self.comms.goals.todos.get(task), unit)
        line = (f'{time.strftime("%Y-%m-%d %H:%M")} {unit} (strike voided on {task}, '
                f'recorded {hhmm(voided.at)}; {n} strike(s) remain): {why}')
        self.on_record(line, 'Void', VerdictAudience.of(unit, todo.goal))
        print(f'{unit}: {n} strike(s) on {task}')

    def due(self, pid: str, at: str, why: str) -> None:
        """Core moves a held order's deadline, its assignment unchanged; the reason goes on record."""
        todo = self.comms.goals.todos.get(pid)
        if not todo.pending:
            sys.exit(f'Refused: {pid} holds no pending order whose deadline could move.')
        if len(why.split()) < 8:
            sys.exit('Refused: --why says why the deadline moves (8+ words)')
        refuse(why, *OUTGOING)  # posted to #comms, so refused before Core records the deadline
        moved = deadline(at)
        self.call(CommsChangeProblemTool(problem=pid, operation=AssignTodo(
            todo.revision, todo.assignment, moved, todo.order, todo.goal)))
        self.on_record(f'{time.strftime("%Y-%m-%d %H:%M")} {todo.holder} ({pid} order {todo.order.id} due '
                  f'{hhmm(todo.deadline)} moved to {hhmm(moved)}): {why}', 'Deadline',
                  VerdictAudience.of(todo.held().owner, todo.goal))
        print(f'{pid}: due {hhmm(moved)}')

    @boundary_decoder
    def withdraw(self, pid: str, why: str, superseded_by: str | None = None) -> None:
        """Core retires an undelivered order sent in error.

        The reason goes on the public record and to the unit that held it."""
        todo = self.comms.goals.todos.get(pid)
        todo.order.require_withdrawal()
        refuse(why, *OUTGOING)  # posted to #comms and the holder, so refused before Core records it
        self.call(CommsChangeProblemTool(problem=pid, operation=WithdrawTodo(
            todo.revision, self.incarnation, todo.held(), why,
            superseded_by)))
        unit = todo.held().owner
        withdrawal, = self.comms.goals.todos.get(pid).order.withdrawals(todo)
        self.on_record(f'{time.strftime("%Y-%m-%d %H:%M")} {withdrawal}', 'Withdrawal',
                  VerdictAudience.of(unit, todo.goal))
        self.post(VerdictAudience((unit,)),
             f'Withdrawn: {withdrawal}. Stop that work; it counts against no one.')
        self.write_board()
        print(withdrawal)

    def fire(self, unit: str, why: str) -> None:
        """Core releases every open problem the unit holds in one call; orders and strikes stay."""
        reason = StrikeReason(why)
        for todo in self.call(CommsFireTool(unit=unit, reason=reason)).todos:
            print(f'{todo.id}: released from {unit}: {why}')


@dataclass(frozen=True, kw_only=True)
class CommanderVerb(CommanderRole, DeclaredFamily, DeclaredOptions, affix="Verb"):
    """One commander verb: its options are its fields, its parser comes from them, and run() does the work."""
    fork: str | None = option("--fork", default=None, help="a forked commander's name; keeps its beat fresh")

    def who(self, ctx: Commander) -> str:
        return self.fork or ctx.commander

    def apply(self, ctx: Commander) -> None:
        self.gate(ctx)
        self.run(ctx)

    def gate(self, ctx: Commander) -> None:
        self.require_commander(ctx.comms.registry.require(self.who(ctx)))
        if self.fork:
            capacity.beat(ctx.state / 'forks', self.fork)

    @abstractmethod
    def run(self, ctx: Commander) -> None: ...

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
        return cls.member_named(args.command).from_arguments(args)


class RuledVerb(CommanderVerb):
    """HARD RULE: no commander action, mine or a fork's, on rules not re-read in 20 min."""

    def gate(self, ctx: Commander) -> None:
        super().gate(ctx)
        for due in RulesDue.violations(ctx.comms, self.who(ctx)):
            flag = '' if self.who(ctx) == ctx.commander else f' --fork {self.who(ctx)}'
            sys.exit(f'REFUSED, nothing recorded: {due.why}. Run agent-comms-commander'
                     f' rules review{flag}, then answer each drawn rule{flag}.')


class OrderVerb(CommanderVerb):
    """A verb of `order`."""


@dataclass(frozen=True, kw_only=True)
class SendVerb(OrderVerb, RuledVerb):
    help = "record the order in Core and send it"
    unit: str = option("unit")
    due: str = option("due", help="HH:MM local time")
    body_file: str = option("body_file", metavar="BODYFILE")
    clean: bool = option("--clean", default=False, help="the unit showed no incompetence")
    mechanical: bool = option("--mechanical", default=False)
    reassign: str | None = option("--reassign", default=None, help="the concrete failure that forces replacement")
    replaces: str | None = option("--replaces", default=None, help="the current message this order replaces")
    disk: bool = option("--disk", default=False, help="a cleanup order; never overrides a tool's storage rule")
    traced: str | None = option("--traced", default=None, metavar="PATH:LINE",
                                help="the first failing source line, required after two owners")

    def run(self, ctx: Commander) -> None:
        ctx.send(self.unit, self.due, Path(self.body_file).read_text(), clean=self.clean,
             mechanical=self.mechanical, reassign=self.reassign, traced=self.traced,
             replaces=self.replaces, disk=self.disk)


@dataclass(frozen=True, kw_only=True)
class StrikeVerb(OrderVerb, RuledVerb):
    help = "a send-back, a missed deadline or a false claim"
    unit: str = option("unit")
    why: str = option("--why")
    caught: str = option("--caught", default='review', help='"WHO at STAGE"; stage is review/install/live/user')

    def run(self, ctx: Commander) -> None:
        references = set(re.findall(r'\bP\d+\b', self.why))
        if len(references) != 1:
            sys.exit('Refused: name exactly one problem in --why; strikes count per problem.')
        ctx.strike(self.unit, references.pop(), self.why, self.caught)


@dataclass(frozen=True, kw_only=True)
class FireVerb(OrderVerb, RuledVerb):
    help = "take every open problem off a unit; their orders and strikes stay"
    unit: str = option("unit")
    why: str = option("--why")

    def run(self, ctx: Commander) -> None:
        ctx.fire(self.unit, self.why)


@dataclass(frozen=True, kw_only=True)
class UnstrikeVerb(OrderVerb, RuledVerb):
    help = "void a strike recorded in error; it stays on the record and stops counting"
    pid: str = option("pid")
    why: str = option("--why")
    at: str | None = option("--at", default=None,
                            help="HH:MM the strike was recorded; default the newest")

    def run(self, ctx: Commander) -> None:
        ctx.unstrike(self.pid, self.why, self.at)


@dataclass(frozen=True, kw_only=True)
class DueVerb(OrderVerb, RuledVerb):
    help = "move a held order's deadline; the reason goes on the public record"
    pid: str = option("pid")
    at: str = option("at", help="HH:MM the order is now due")
    why: str = option("--why")

    def run(self, ctx: Commander) -> None:
        ctx.due(self.pid, self.at, self.why)


@dataclass(frozen=True, kw_only=True)
class InstallVerb(RuledVerb):
    help = "plan the fleet's next install; merged and delivered orders wait on it"
    at: str = option("at", help="HH:MM the install is due")
    installer: str = option("installer", help="the unit that runs the install")

    def run(self, ctx: Commander) -> None:
        PlannedInstallDeadline(deadline(self.at), self.installer).plan(ctx.comms.root)
        print(f'install due {self.at}, run by {self.installer}')


@dataclass(frozen=True, kw_only=True)
class WithdrawVerb(OrderVerb, RuledVerb):
    help = "retire an undelivered order sent in error; its problem closes with no strike"
    pid: str = option("pid")
    why: str = option("--why")
    superseded_by: str | None = option("--superseded-by", default=None, metavar="PID",
                                       help="the problem that already owns this fix")

    def run(self, ctx: Commander) -> None:
        ctx.withdraw(self.pid, self.why, self.superseded_by)


@dataclass(frozen=True, kw_only=True)
class DoneVerb(OrderVerb, RuledVerb):
    help = "Core checks the delivered commit; late delivery is a strike"
    unit: str = option("unit", metavar="UNIT")
    message: str = option("message", metavar="ORDER")
    sha: str = option("--sha", help="the delivered commit in --repo HEAD, or op")
    screenshot: str = option("--screenshot", default="", metavar="PATH",
                             help="for an op: the screenshot the working claim stands on")

    seen: str = option("--seen", default="")
    repo: str = option("--repo", default="", help="the recorded order check directory")

    def run(self, ctx: Commander) -> None:
        todo = ctx.comms.goals.todos.resolve_order(ctx.incarnation, self.unit, self.message)
        ctx.done(todo.id, self.sha, self.screenshot, self.seen, self.repo)


@dataclass(frozen=True, kw_only=True)
class PullRequestVerb(OrderVerb, declared_name="pr"):
    help = "open the problem's PR with its required immutable order identity"
    pid: str = option("pid")
    title: str = option("--title")
    body_file: str = option("--body-file")

    def run(self, ctx: Commander) -> None:
        todo = ctx.comms.goals.todos.get(self.pid)
        checkout = ctx.comms.registry.storage_rule.order_checkout(todo.holder, todo.id)
        print(todo.order.open_pull_request(
            todo, checkout, self.title, Path(self.body_file).read_text()))


@dataclass(frozen=True, kw_only=True)
class CheckVerb(OrderVerb, RuledVerb):
    help = "replace an open order's wrong check with one passing on main; keeps deadline, strikes"
    pid: str = option("pid")
    check: str = option("--check", help='"<repo> :: <shell command> -> <integer>"')
    why: str = option("--why", help="what was wrong with the recorded check")

    def run(self, ctx: Commander) -> None:
        ctx.correct_check(self.pid, self.check, self.why)


@dataclass(frozen=True, kw_only=True)
class ServeVerb(OrderVerb, RuledVerb):
    help = "name the plan phase an open order serves; keeps its id, deadline and strikes"
    pid: str = option("pid")
    phase: str = option("phase", help="one of " + ", ".join(OrderPhase.names()))

    def run(self, ctx: Commander) -> None:
        ctx.call(CommsOrderServeTool(problem=self.pid, serves=OrderPhase.member_named(self.phase)))
        print(f'{self.pid} serves {self.phase}')


@dataclass(frozen=True, kw_only=True)
class SayVerb(OrderVerb, RuledVerb):
    help = "a non-order message; refused if it orders work"
    units: str = option("units", metavar="UNIT[,UNIT]")
    body_file: str = option("body_file", metavar="BODYFILE")

    def run(self, ctx: Commander) -> None:
        ctx.say(self.units, self.body_file)


@dataclass(frozen=True, kw_only=True)
class ProblemVerb(OrderVerb, RuledVerb):
    help = "Core records the problem with its order, or nothing"
    text: str = option("text")
    to: str = option("--to")
    due: str = option("--due", help="HH:MM local time")
    order: str = option("--order", help="the order body file")
    where: str = option("--where", help="the actual file:line where this problem occurs")
    disk: bool = option("--disk", default=False)

    def run(self, ctx: Commander) -> None:
        ctx.problem(self.text, self.to, self.due, self.order, self.where, self.disk)


@dataclass(frozen=True, kw_only=True)
class DropVerb(OrderVerb, RuledVerb):
    help = "drop an old order for a live newer one without a strike"
    unit: str = option("unit")
    message: str = option("message")
    by: str = option("--by")

    def run(self, ctx: Commander) -> None:
        ctx.comms.goals.todos.drop(ctx.incarnation, self.unit, self.message, self.by)
        print(f'{self.unit}: {self.message} replaced by {self.by}')


@dataclass(frozen=True, kw_only=True)
class FoldVerb(OrderVerb, RuledVerb):
    help = "join duplicate open problems without losing their text"
    pid: str = option("pid")
    into: str = option("--into")
    why: str = option("--why")

    def run(self, ctx: Commander) -> None:
        ctx.comms.goals.todos.fold(ctx.incarnation, self.pid, self.into, self.why)
        print(f'{self.pid} folded into {self.into}')


@dataclass(frozen=True, kw_only=True)
class LinkVerb(OrderVerb, RuledVerb):
    help = "attach an unassigned problem to an existing order"
    pid: str = option("pid")
    unit: str = option("unit")
    message: str = option("message")

    def run(self, ctx: Commander) -> None:
        ctx.comms.goals.todos.link(ctx.incarnation, self.pid, self.unit, self.message)
        print(f'{self.pid} held by {self.unit} through {self.message}')


@dataclass(frozen=True, kw_only=True)
class CloseVerb(OrderVerb, RuledVerb):
    help = "the fix works in the app; Core closes the problem"
    pid: str = option("pid")
    works: str = option("--works")
    artifact: str = option("--artifact", default="", help="defaults to the file recorded by done --seen")

    def run(self, ctx: Commander) -> None:
        ctx.close(self.pid, self.works, self.artifact)


@dataclass(frozen=True, kw_only=True)
class HuntVerb(OrderVerb, RuledVerb):
    help = "a hot-path read: clean unless --sent names the order it became"
    where: str = option("where", metavar="FILE:LINE")
    why: str = option("--why")
    sent: str = option("--sent", default="", metavar="ORDER",
                       help="the open order the hunt became; omitted means clean")

    @classmethod
    def due(cls, where: str | None) -> str:
        return (f'HUNT DUE (none in 15 min): read {where} for per-event or per-history work '
                f'or slop, then {cls.usage()}')

    def run(self, ctx: Commander) -> None:
        ctx.hunt(self.where, self.why, self.sent)


class NextHuntVerb(OrderVerb, declared_name="next-hunt"):
    help = "print the next place to hunt"

    def run(self, ctx: Commander) -> None:
        print(ctx.next_hunt())


class PeerSyncVerb(OrderVerb, declared_name=PeerSync.verb):
    """Run the hook against the selected commander's existing context and session owner."""

    help = "inject or block when the selected commander owes its peer a relay"

    def run(self, ctx: Commander) -> None:
        hook = ClaudeHook.read()
        if CommanderSession(hook.session_id, ctx).is_commander():
            PeerSync.report(hook, ctx)


class BoardVerb(OrderVerb):
    help = "show the fleet board generated from Core's Todo store"

    def run(self, ctx: Commander) -> None:
        ctx.write_board()
        print(ctx.board.read_text(), end='')


class OverdueVerb(OrderVerb):
    help = "loop: every minute, name units past their deadline"

    def run(self, ctx: Commander) -> None:
        ctx.overdue()


def main(a, ctx: Commander):
    OrderVerb.parse(a, __doc__).apply(ctx)
