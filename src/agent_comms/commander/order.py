"""The only way the commander sends an order to a unit. Refuses soft or vague orders; records strikes,
and starts the commander's review timer on every order.

`order send` and `order problem` take one JSON file. Its `order` is decoded by
FieldCodec into the PendingProblemOrder used by native tools; its other fields
name the problems and explain the defect. No rendered instructions are parsed.
Sending to the unit that already holds the problem replaces its order; without --clean that is a send-back strike.

Incompetence is not tolerated: a unit is fired from a task, not berated about it.
Two strikes on one task: the task is taken away and reassigned (`fire`). A strike recorded in
error is voided with `unstrike`, never deleted: it stays readable beside its void and stops
counting. An order sent in error is retired with `withdraw`: its problem closes with no strike.
Nothing here judges code; it refuses orders that leave a unit room to interpret. The unit never
sees the review time: when it is up, the heartbeat tells the commander to read the diff.
"""
from __future__ import annotations

import argparse
import dataclasses
import re
import subprocess
import sys
import time
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar, Self

from pydantic import Field, InstanceOf, PrivateAttr

from agent_comms.change_report import FileLine, MainRevision, NoSourceReference
from agent_comms.cli_bridges.shared import DeclaredVerb
from agent_comms.commander.config import CommanderConfig, CommanderRole
from agent_comms.commander.standing import UnownedWheelSpin
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import (
    Argument,
    DecodedValue,
    FieldCodec,
    MalformedRecord,
    Normalize,
    NullMember,
    SingleFieldValue,
    SubclassUnion,
    boundary_decoder,
)
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction
from agent_comms.goals import OrderCheck, ProblemGoalOrder
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.owner_requests import NewestOpenRequest, RequestSelection
from agent_comms.plan_phases import OrderPhase
from agent_comms.problem_number import ProblemNumber
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.standing_rules import HuntDue, OwedAnswer
from agent_comms.storage_rules import UnitStorage
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import (
    BluntText,
    CheckedDelivery,
    CleanHunt,
    CommitDelivery,
    DeliveryEvidence,
    DeliverySource,
    DirectiveText,
    LocatedProblemFinding,
    MeasuredReview,
    NoSuccessor,
    OperationDelivery,
    OrderDelivery,
    OrderedHunt,
    OrderMessage,
    OrderProgress,
    OrderText,
    OrderWords,
    PendingProblemOrder,
    PullRequestLink,
    RecordPullRequest,
    Slop,
    StrikeReason,
    StrikeTodo,
    Successor,
    Todo,
    UnlocatedProblemFinding,
    VerdictAudience,
    VoidStrikeTodo,
    WithdrawTodo,
)
from agent_comms.tool_caller import CommanderForkCaller, NoThreadCaller
from agent_comms.tools import (
    CommsChangeProblemTool,
    CommsFireTool,
    CommsOrderCheckTool,
    CommsOrderDoneTool,
    CommsOrderServeTool,
    CommsOrderTool,
    CommsProblemCloseTool,
    CommsTodoCreateTool,
    CommsTodoListTool,
    TodoList,
)

OUTGOING = DirectiveText.OUTGOING  # every commander text passes each one


def refuse(text, *families):
    """Core's text families decide what the commander may write; nothing is recorded when one refuses."""
    if refused := [r for family in families if (r := family.refused(text))]:
        sys.exit('REFUSED, nothing recorded: ' + ' '.join(refused))


def struck(todo, unit):
    """Strikes count per unit and task, across every order of that task the unit held."""
    return len(todo.live_strikes(unit))


# Eliminate forwarding with inheritance and multiple parents; decompose the hierarchy if it is not sound.
# A forwarder restates an implementation pair (paper 1 Fig 3: fields + forwarding leave routes to maintain), so
# nothing the commander approves, delivers or orders may add or keep one. The cure is the hierarchy, never a
# route: fold the false owner into the real one, or compose the providers as parents.
FORWARDER = re.compile(
    r"^\+\s*(?:def \w+\([^)]*\)(?:\s*->[^:]+)?:\s*return self\.\w+\.\w+(?:\([^()]*\))?\s*$"  # one-line def returning self.x.y(...)
    r"|\w+\s*=\s*property\(lambda \w+:\s*\w+\.\w+\.\w+\)"  # attr = property(lambda s: s._a.attr)
    r"|def __getattr__\(self)",
    re.M,
)
FORWARD_BODY = re.compile(
    r"^\+(\s*)def (\w+)\(self[^)]*\)[^:]*:\s*\n\+\1\s+return self\.\w+\.\2\b", re.M
)  # def x(self): / return self._a.x(...)


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


class CommandDeliverySource(DeliverySource, declared_name=None):
    @classmethod
    def from_text(cls, text: str) -> CommandDeliverySource:
        # Reserved arguments derive from declarations; every other token is a Git reference.
        return ArgumentDeliverySource.member_or_default(text, ReferenceDeliverySource).from_argument(text)


class ArgumentDeliverySource(CommandDeliverySource, declared_name=None):
    @classmethod
    def from_argument(cls, text: str) -> Self:
        return cls()


@dataclass(frozen=True)
class ReferenceDeliverySource(ArgumentDeliverySource, declared_name=None):
    reference: str

    @classmethod
    def from_argument(cls, text: str) -> Self:
        if not text.strip():
            raise ValueError("A commit delivery needs a nonempty Git reference.")
        return cls(text)


@dataclass(frozen=True)
class NoDeliverySource(CommandDeliverySource):
    def require_report(self) -> None:
        pass

    def delivery(self, repo: Path, evidence: DeliveryEvidence) -> CheckedDelivery:
        raise ValueError("A fix delivery needs --sha COMMIT or --sha op.")


@dataclass(frozen=True)
class OpDeliverySource(ArgumentDeliverySource):
    def delivery(self, repo: Path, evidence: DeliveryEvidence) -> CheckedDelivery:
        return OperationDelivery(evidence=evidence)


@dataclass(frozen=True)
class CommitDeliverySource(ReferenceDeliverySource):
    def delivery(self, repo: Path, evidence: DeliveryEvidence) -> CheckedDelivery:
        delivered = Repo.git(repo, 'rev-parse', '--verify', f'{self.reference}^{{commit}}').strip()
        diff = Repo.git(repo, 'show', '--first-parent', '-m', '--format=', delivered)
        changed = re.findall(r'^\+\+\+ b/(\S+\.py)$', diff, re.M)
        sources = [Repo.git(repo, 'show', f'{delivered}:{path}') for path in changed]
        if added := forwarders_added(diff, sources):
            raise ValueError(f'Refused: {delivered} adds forwarders {added[:5]}; '
                             'slop never counts as delivery. Order the hierarchy fix.')
        return CommitDelivery(
            commit=Repo.git(repo, 'rev-parse', '--verify', 'HEAD^{commit}').strip(),
            delivered_commit=delivered, evidence=evidence,
        )


@dataclass(frozen=True)
class OrderFile:
    """CLI context and prose around the same order the native tools accept."""

    order: PendingProblemOrder
    defect: str
    why_stupid: str
    unplanned_reason: str = ""
    problems: tuple[str, ...] = ()
    slop: tuple[Slop, ...] = ()

    @classmethod
    def read(cls, text: str):
        try:
            return FieldCodec.decode(cls, FieldCodec.parse(text))
        except MalformedRecord as error:
            sys.exit(f'Refused order, nothing recorded:\n{error}')

    @property
    def prose(self) -> str:
        reason = f'\nUnplanned reason: {self.unplanned_reason}' if self.unplanned_reason else ''
        return f'{self.defect}\n{self.why_stupid}{reason}'

    def problems_found(self, clean: bool) -> list[str]:
        wording: tuple[type[OrderWords], ...] = (*OUTGOING, OrderText)
        problems = [f'{name}: {error}; fix: state the defect plainly without wording rules on code'
                    for name, text in (('defect', self.defect), ('why_stupid', self.why_stupid))
                    for family in wording
                    if (error := family.refused(text))]
        if len(self.defect.split()) < 8:
            problems.append('defect: fix: describe what fails in at least eight words')
        if not self.why_stupid.strip():
            problems.append('why_stupid: fix: explain why this design is wrong')
        if not clean and (timid := BluntText.refused(self.prose)):
            problems.append(timid + '; fix: name the defect in why_stupid, or use --clean')
        if re.search(r'\bslop\b', self.prose, re.I) and not self.slop:
            problems.append('slop: fix: supply pattern, fact, at, derive and by citation fields')
        return problems


STAGES = (
    "tool call",
    "review",
    "install",
    "live",
    "user",
)  # where a mistake was caught; later is worse


class CommanderContext(CommanderConfig):
    """The selected root's bus and canonical reads, shared by command families."""

    actor: Annotated[
        InstanceOf[CommanderForkCaller] | InstanceOf[NoThreadCaller], Field(exclude=True)
    ] = NoThreadCaller()
    _comms: Comms = PrivateAttr()

    def model_post_init(self, context: object) -> None:
        self._comms = Comms(self.root)

    @boundary_decoder
    def model_copy(self, *, update: Mapping[str, object] | None = None,
                   deep: bool = False) -> Self:
        """Normalize Pydantic's optional updates; copy values, never live resources."""
        values = {name: getattr(self, name) for name in type(self).model_fields}
        if deep:
            values = deepcopy(values)
        changes = update or {}
        values.update(changes)
        return type(self).model_construct(
            _fields_set=self.model_fields_set.union(changes), **values)

    @property
    def comms(self) -> Comms:
        return self._comms

    @property
    def incarnation(self) -> ThreadIncarnation:
        return self.comms.registry.require(self.commander).incarnation

    @property
    def actor_name(self) -> str:
        return self.actor.acting_name(self.commander)

    @property
    def actor_identity(self) -> ThreadIncarnation:
        return self.actor.as_thread(self.commander).identity(self.comms)

    def post(self, audience: VerdictAudience, body: str,
             answers: tuple[MessageReference, ...] = ()) -> str:
        target = audience.target
        published = self.comms.messaging.send(self.actor_name, target.name,
            target.addressed(audience.threads, body), answers=answers)
        return published.message.message_id

    def call(self, tool):
        return self.actor.as_thread(self.commander).invoke(tool, self.comms)

    def todos(self):
        return self.comms.goals.todos.for_commander(self.incarnation)

    def read_check(self, line: str) -> OrderCheck:
        if not (check := OrderCheck.parse(line)):
            sys.exit('Refused: Check reads "<repo> :: <shell command> -> <integer>"')
        directory = self.repo_directory(check.directory)
        if not directory.is_dir():
            sys.exit(f'Refused: Check repo {check.directory} is not a directory')
        return replace(check, directory=str(directory))

    def held_orders(self) -> tuple[Todo, ...]:
        return tuple(todo for todo in self.todos() if todo.holder)

    def held_by(self, unit: str) -> tuple[Todo, ...]:
        return self.comms.goals.todos.held_by(
            self.incarnation, self.comms.registry.require(unit).incarnation)

    def units(self) -> frozenset[str]:
        return self.comms.goals.todos.units(self.incarnation)

    def progress(self) -> dict[str, OrderProgress]:
        listed: TodoList = self.call(CommsTodoListTool(thread=self.commander))
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
        thread = self.comms.registry.require(unit)
        return thread.runtime.history_source(self.comms.root, thread.incarnation).last_tool_call()

    def board_text(self) -> str:
        """Render the live Todo view without saving a competing board file."""
        lines = ['# Fleet board', '',
                 "Live view of Core's Todo store; no board file is saved.", '',
                 '| Problem | Owner file | Owner | Order | Due | Check | Progress | Order state '
                 '| Strikes | Owner last acted |',
                 '|---|---|---|---|---|---|---|---|---|---|']
        listed, read = self.todos(), self.progress()
        activity = {unit: self.last_activity(unit) for unit in {t.holder for t in listed if t.holder}}
        for t in sorted(listed, key=lambda t: ProblemNumber.parse(t.id).number):
            lines.append(f'| {t.id}: {t.text.replace("|", "&#124;")} | {t.order.board_cells(t)} | '
                         f'{read[t.id].line if t.id in read else "-"} | {t.order.line} | '
                         f'{t.strike_count} | {activity[t.holder].clock() if t.holder else "-"} |')
        history = self.comms.goals.todos.history_for_commander(self.incarnation)
        lines += ['', '## Withdrawn orders', '',
                  *(f'- {line}' for t in history for line in t.order.withdrawals(t))]
        return '\n'.join(lines) + '\n'


class Reassignment(SubclassUnion, affix="Reassignment"):
    """Why a held problem changes owner, or no change of owner."""

    # A resend to the same unit is a send-back strike unless it reassigns.
    strikes_send_back: ClassVar[bool]

    @abstractmethod
    def refusals(self, holders: Sequence[str]) -> tuple[str, ...]: ...

    @abstractmethod
    def note(self) -> str:
        """The order's line saying why the unit takes the work over."""

    @abstractmethod
    def because(self) -> str:
        """The reason told to each former holder."""


@dataclass(frozen=True, slots=True)
class ReassignReason(SingleFieldValue, Reassignment):
    text: str
    strikes_send_back = False

    def refusals(self, holders: Sequence[str]) -> tuple[str, ...]:
        return () if len(self.text.split()) >= 8 else (
            '--reassign needs the concrete failure that forces replacement (8+ words), '
            'told to the old and new owner',)

    def note(self) -> str:
        return f'\nTaken over because: {self.text}'

    def because(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class NoReassignment(NullMember, Reassignment):
    strikes_send_back = True

    def refusals(self, holders: Sequence[str]) -> tuple[str, ...]:
        return () if not holders else (
            f'fact already owned by {list(holders)}; keep that owner, or pass --reassign '
            '"<the concrete failure: provider diagnostic, strikes, a wrong delivery>"',)

    def note(self) -> str:
        return ''

    def because(self) -> str:
        raise RelationViolationError('a held problem changes owner only with --reassign')


class StrikeSelector(SubclassUnion, affix="Strike"):
    """Which live strike a void takes back: the one recorded at a clock time, or the newest."""

    @abstractmethod
    def selects(self, strike: StrikeTodo) -> bool: ...

    @abstractmethod
    def refusal(self, task: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ClockedStrike(SingleFieldValue, StrikeSelector):
    clock: str

    def selects(self, strike: StrikeTodo) -> bool:
        return strike.at.clock() == self.clock

    def refusal(self, task: str) -> str:
        return f'Refused: {task} has no live strike recorded at {self.clock}.'


@dataclass(frozen=True, slots=True)
class NewestStrike(NullMember, StrikeSelector):
    def selects(self, strike: StrikeTodo) -> bool:
        return True

    def refusal(self, task: str) -> str:
        return f'Refused: {task} has no live strike.'


class Commander(CommanderContext):
    @boundary_decoder
    def send(self, unit, document: OrderFile, clean=False,
             reassign: Reassignment = NoReassignment(), new=None,
             traced: FileLine | NoSourceReference = NoSourceReference(),
             disk=False, finding=UnlocatedProblemFinding(),
             answers: tuple[MessageReference, ...] = (), *, request: RequestSelection):
        """Record the decoded order, or report every failed prerequisite before any write."""
        problems = document.problems_found(clean)
        check = replace(document.order.check,
                        directory=str(self.repo_directory(document.order.check.directory)))
        order = replace(document.order, finding=finding, check=check)
        edits = order.edits
        body = '\n'.join((document.prose, *edits, order.report))
        # Below the floor Core's StorageRule denies each allocating tool call, so the order prose is not checked.
        try:
            self.comms.registry.storage_rule.require_quota(self.comms.registry.require(unit))
        except SelectedToolDenied as quota:
            problems.append(f'{unit}: {quota}: it cleans up its own checkouts and scratch before it gets new work')
        group = [new[0]] if new else list(document.problems)
        listed = {t.id: t for t in self.todos()}
        reading = self.plan_reading(self.comms.goals.todos.history_for_commander(self.incarnation))
        problems.extend(order.phase.order_refusals(reading, document.unplanned_reason))
        held = tuple(todo for todo in listed.values() if todo.holder)
        busy = tuple(todo for todo in held if todo.holder == unit and todo.id not in group)
        if busy:
            snapshot = self.comms.registry.snapshot()
            occupied = {todo.holder for todo in held} | {self.commander} | self.parked
            idle = sorted(thread.name for thread in snapshot.threads.values()
                          if thread.name not in occupied and thread.role.executor(thread)
                          and snapshot.status(thread.name).active)
            problems.append(f'{unit} already holds orders '
                            f'{", ".join(todo.id for todo in busy)}; '
                            f'running units holding no order: {", ".join(idle) or "none"}')
        if not group or any(not ProblemNumber.accepts(pid) for pid in group):
            problems.append('problems: fix: supply an array of P<n> IDs, or use order problem')
        if new and document.problems:
            problems.append('problems: fix: omit IDs when creating a new problem')
        if not new and (missing := [pid for pid in group if pid not in listed]):
            problems.append(f'{missing} not open problems; fix: name existing open problem IDs')
        for label, validate in (
            ('order.source', order.require_references),
            ('order.check.directory', check.require_directory),
        ):
            try:
                with MalformedRecord.refusing(label):
                    validate()
            except MalformedRecord as error:
                problems.append(f'{label}: {error}; fix: name an existing source location or directory')
        # The new order owns the source against which its check must fail.
        try:
            with MalformedRecord.refusing('order.check'):
                order.require_writable()
                if Path(check.directory).is_dir():
                    Repo.git(Path(check.directory), 'fetch', '-q', 'origin', 'main')
                    base = order.check_base(Path(check.directory))
                    check.require_acceptance(base, self.worktrees)
        except MalformedRecord as error:
            problems.append(f'order.check: {error}; fix: supply a focused measurable command')
        references = list(dict.fromkeys((
            *FileLine.scan('\n'.join((document.prose, *edits))),
            *(reference for slop in document.slop for reference in (slop.at, slop.by)),
        )))
        deleted = {Path(module).stem for module in order.deletes.modules}
        if check and deleted and (unread := self.importers(deleted, check)):
            problems.append(f'the order deletes {sorted(deleted)}, which {unread} import; '
                            'the check must also count their `from agent_comms` imports')
        # Ownership reversals waste work: one owner through completion; a change of owner names the concrete failure.
        holders = sorted({listed[pid].holder for pid in group if pid in listed} - {unit, None})
        replacing = not new and bool(group) and all(pid in listed and listed[pid].holder == unit for pid in group)
        references.extend(traced.located())
        for reference in references:
            try:
                with MalformedRecord.refusing(reference.to_wire()):
                    reference.require_exists(Path(check.directory), order.cited_revision)
            except MalformedRecord as error:
                problems.append(f'{error}; fix: cite an existing source line')
        if any((Path(check.directory) / reference.file).resolve().is_relative_to(
                   Path(check.directory) / 'tools/')
               for reference in references) and not all(
                said in body for said in ('--root tools', '--root src')):
            problems.append('the order changes tools/: say "ratchet with --root tools and with '
                            '--root src, each as its own command"')
        for pid in group:
            try:
                with MalformedRecord.refusing(pid):
                    if pid in listed:
                        listed[pid].holder_state.require_trace(unit, traced)
            except MalformedRecord as error:
                problems.append(str(error))
        problems.extend(reassign.refusals(holders))
        if problems:
            sys.exit('Refused order:\n  ' + '\n  '.join(problems))
        instructions = '\n'.join((document.prose, *(slop.render() for slop in document.slop),
            f'Problems: {", ".join(group)}', *order.work_instructions()))
        message = (instructions + ('\nThis replaces my earlier order on it; drop that one.' if replacing else '') + reassign.note() + '\n' + self.comms.registry.storage_rule.order_text(self.comms.registry.require(unit), [f'{repo.checkout}/.venv/bin/python' for repo in self.repos]) + '\nIf two lines of this order conflict, take the narrower reading, say which one you took, and proceed; do not stop to ask.'
                   + '\nStart now and push as you go.')
        taken = {other: f'Taken off you: {", ".join(group)} ("{listed[group[0]].text[:120]}") now belongs to {unit} because: {reassign.because()}. Stop that work; do not touch its files.' for other in holders}
        fired = tuple(listed[pid] for pid in group
                      if pid in listed and listed[pid].struck_out(unit))
        order.run.require_assignment(self.comms.registry.require(unit).incarnation,
                                     fired, ProblemGoalOrder.StrikeLimit)
        # A miss already struck on the order this replaces is one miss:
        # the send-back records no second strike.
        struck_already = replacing and listed[group[0]].struck_on_order(unit)
        measured = MeasuredReview.read(self.comms.goals.todos)
        review_at = measured.review_at(order)
        request_id = request.for_order(self.comms.goals.todos, self.incarnation)
        if new:
            self.call(CommsTodoCreateTool(owner=unit, order=CreateProblemGoalAction(
                problem=new[0], repo=Repo.of(Path(check.directory)).github, text=new[1],
                order=order, request=request_id, review_at=review_at, commander=self.commander,
                assigner=self.actor_identity)))
        for pid in [] if new else group:
            self.call(CommsOrderTool(owner=unit, order=OrderGoalAction(
                problem=pid, expected_revision=self.comms.goals.todos.get(pid).revision,
                assigner=self.actor_identity, traced=traced,
                order=order, request=request_id, review_at=review_at, commander=self.commander)))
        message += "\n" + "\n".join(line for pid in group for line in order.scope.instructions(pid))
        sent = self.post(VerdictAudience((unit,)), message, answers)
        # The message is each order's recorded link; a verdict citing it lands on that order.
        for pid in group:
            self.call(CommsChangeProblemTool(problem=pid, operation=OrderMessage(
                self.comms.goals.todos.get(pid).revision, self.actor_identity, sent)))
        for other, text in taken.items():
            self.post(VerdictAudience((other,)), text)
        print(f'{measured.summary}; review timer {review_at.clock()}')
        print(order.id)
        if replacing and not clean and struck_already:
            print(f'{unit} already struck on this order of {group[0]}; '
                  'the send-back counts that one miss once')
        elif replacing and not clean and reassign.strikes_send_back:
            # A send-back is a strike, no warnings, and every send-back is roasted in #comms.
            # Done here so the commander cannot send work back and forget either.
            verdict = document.prose.strip()
            self.strike(unit, group[0], f'sent back on {group[0]}: {verdict}',
                        evidence=(order.owner,))

    def correct_check(self, pid: str, line: str, why: str) -> None:
        """Correct the current check against the source preceding this order's delivery."""
        todo = self.comms.goals.todos.get(pid)
        order = todo.order.checked()
        check = self.read_check(line)
        if len(why.split()) < 8:
            sys.exit('Refused: --why says what was wrong with the recorded check (8+ words)')
        repo = self.repo_directory(check.directory)
        Repo.git(repo, 'fetch', '-q', 'origin', 'main')
        check.require_acceptance(order.check_base(repo), self.worktrees)
        self.call(CommsOrderCheckTool(problem=pid, check=check, why=why))
        print(f'{pid}: order {order.id} now checks `{check.command}`')

    def say(self, units: str, body_file: str,
            answers: tuple[MessageReference, ...] = ()) -> None:
        body = open(body_file).read()
        if len(re.findall(r'(?m)^\s*\d+[.)] ', body)) >= 2:
            sys.exit('Refused: an edit list needs a tracked order and review timer; use order send with a JSON file.')
        refuse(body, *OUTGOING)
        print(self.post(VerdictAudience(tuple(units.split(','))), body.rstrip(), answers))

    def close(self, pid, works, artifact=''):
        """Core owns closing: the installed evidence, the artifact and the no-open-order rule live on the Todo."""
        evidence = artifact or self.comms.goals.todos.get(pid).order.closing_artifact()
        self.call(CommsProblemCloseTool(problem=pid, works=works, artifact=evidence))
        print(f'{pid} closed')

    def on_record(self, line: str, recorded: str, audience: VerdictAudience,
                  answers: tuple[MessageReference, ...] = ()) -> None:
        """Publish what Core already recorded; no second narrative file.

        Core already holds what `recorded` names, so a failed post is reposted, never recorded again.
        """
        try:
            self.post(audience, f'On the record: {line}', answers)
        except (SystemExit, ValueError) as failed:
            print(f'{recorded} recorded, but the post failed ({failed}). '
                  'Repost with order.post; do not record it again.', file=sys.stderr)
            raise

    def strike(self, unit: str, task: str, why: str, caught: str = 'review', *,
               evidence: tuple[FileLine, ...], kind: type[StrikeTodo] = StrikeTodo,
               answers: tuple[MessageReference, ...] = ()) -> None:
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
        self.recorded(unit, todo, kind(todo.revision, self.actor_identity, todo.held(),
                                      f'{why} (caught by {caught})',
                                      evidence=evidence), answers)

    def recorded(self, unit: str, todo: Todo, strike: StrikeTodo,
                 answers: tuple[MessageReference, ...] = ()) -> None:
        """Core records the strike on the task's Todo, then posts it to #comms."""
        refuse(strike.reason,
            *OUTGOING)  # posted to #comms, so refused before Core records the strike
        if (self.call(CommsChangeProblemTool(problem=todo.id, operation=strike))
                .todo.revision == todo.revision):
            return
        self.posted(unit, self.comms.goals.todos.get(todo.id), strike, answers)

    def posted(self, unit: str, todo: Todo, strike: StrikeTodo,
               answers: tuple[MessageReference, ...] = ()) -> None:
        """Publish a strike Core already recorded on the task's Todo to #comms."""
        n = struck(todo, unit)
        line = (f'{time.strftime("%Y-%m-%d")} {strike.at.clock()} {unit} (strike {n} on {todo.id}'
                f'{f", {todo.id} taken away" if n >= 2 else ""}): {strike.reason}')
        self.on_record(line, 'Strike', VerdictAudience.of(unit, todo), answers)
        print(f'{unit}: {n} strike(s) on {todo.id}' + (f'. Reassign {todo.id} now and freeze its branch.' if n >= 2 else ''))

    def done(self, pid: str, delivery: OrderDelivery) -> None:
        """Storage is checked here; the delivery owns its evidence and claims."""
        todo = self.comms.goals.todos.get(pid)
        todo.state.require_open(todo)
        order = todo.order.checked()
        order.require_delivery(order.id)
        unit = todo.held().owner
        claims = delivery.done_claims(order)
        thread = self.comms.registry.require(unit)
        storage = UnitStorage.read(self.comms.registry.storage_rule, thread)
        for checkout in storage.rule.problem_checkouts(unit, pid):
            if checkout in storage.extra_checkouts:
                sys.exit(f'Refused: {unit} still has the checkout {checkout} for this order; '
                         'storage rule: it removes it before the order counts as done.')
        try:
            storage.require_quota()
        except SelectedToolDenied as quota:
            sys.exit(f'Refused: {unit}: {quota}; storage rule: it cleans up before the order counts as done.')
        self.call(CommsOrderDoneTool(problem=pid,
            order_id=order.id, delivery=delivery, claims=claims))
        print(f'{unit}: order {order.id} delivered as {delivery.work}')

    def hunt(self, where, why, sent):
        """Record the commander's source read, whether or not it became an order."""
        ref = FileLine.from_wire(where)
        directory = Path.cwd()
        ref.require_exists(directory, MainRevision())
        if len(why.split()) < 10:
            sys.exit('Refused: say what the code does per event or per history row, and the fix or why it is clean (10+ words)')
        ref = FileLine(str((directory / ref.file).resolve()), ref.line)
        at = RecordedEventTime(time.time())
        self.comms.goals.todos.record_hunt(self.incarnation,
            OrderedHunt(ref, why, at, sent) if sent else CleanHunt(ref, why, at))
        print(f'hunt recorded: {where} -> {sent or "clean"}')

    def unstrike(self, task: str, why: str, at: StrikeSelector = NewestStrike()) -> None:
        """A strike recorded in error is voided in Core, then announced to #comms."""
        todo = self.comms.goals.todos.get(task)
        live = [s for s in todo.live_strikes() if at.selects(s)]
        if not live:
            sys.exit(at.refusal(task))
        voided = live[-1]
        refuse(why, *OUTGOING)  # posted to #comms, so refused before Core records the void
        self.call(CommsChangeProblemTool(problem=task, operation=VoidStrikeTodo(
            todo.revision, self.incarnation, voided, why)))
        unit = voided.previous.owner
        n = struck(self.comms.goals.todos.get(task), unit)
        line = (f'{time.strftime("%Y-%m-%d %H:%M")} {unit} (strike voided on {task}, '
                f'recorded {voided.at.clock()}; {n} strike(s) remain): {why}')
        self.on_record(line, 'Void', VerdictAudience.of(unit, todo))
        print(f'{unit}: {n} strike(s) on {task}')

    def withdraw(self, pid: str, why: str, superseded_by: Successor = NoSuccessor()) -> None:
        """Core retires an undelivered order sent in error.

        The reason goes on the public record and to the unit that held it."""
        todo = self.comms.goals.todos.get(pid)
        todo.order.require_withdrawal()
        refuse(why, *OUTGOING)  # posted to #comms and the holder, so refused before Core records it
        previous = todo.holder_state.previous_assignment()
        self.call(CommsChangeProblemTool(problem=pid, operation=WithdrawTodo(
            todo.revision, self.incarnation, previous, why,
            superseded_by)))
        unit = previous.owner
        withdrawal, = self.comms.goals.todos.get(pid).order.withdrawals(todo)
        self.on_record(f'{time.strftime("%Y-%m-%d %H:%M")} {withdrawal}', 'Withdrawal',
                  VerdictAudience.of(unit, todo))
        self.post(VerdictAudience((unit,)),
             f'Withdrawn: {withdrawal}. Stop that work; it counts against no one.')
        print(withdrawal)

    def fire(self, unit: str, why: str) -> None:
        """Core releases every open problem the unit holds in one call; orders and strikes stay."""
        reason = StrikeReason(why)
        for todo in self.call(CommsFireTool(unit=unit, reason=reason)).todos:
            print(f'{todo.id}: released from {unit}: {why}')


@dataclass(frozen=True, kw_only=True)
class CommanderVerb(CommanderRole, DeclaredVerb, affix="Verb"):
    """One commander verb: its options are its fields, its parser comes from them, and run() does the work."""

    fork: Annotated[
        CommanderForkCaller | NoThreadCaller,
        Normalize(FieldCodec.encode),
        Argument(
            flags=("--fork",), options={"type": CommanderForkCaller, "help": "the acting thread's name"}
        ),
    ] = NoThreadCaller()

    def who(self, ctx: Commander) -> str:
        return ctx.actor_name

    def apply(self, ctx: Commander) -> None:
        self.require_commander(ctx.comms.registry.require(ctx.commander))
        self.fork.commanding(ctx.comms, ctx.commander)
        ctx = ctx.model_copy(update={'actor': self.fork})
        self.gate(ctx)
        self.run(ctx)

    def gate(self, ctx: Commander) -> None:
        self.require_commander(ctx.comms.registry.require(self.who(ctx)))

    @abstractmethod
    def run(self, ctx: Commander) -> None: ...

    @classmethod
    def usage(cls) -> str:
        """This verb's command line, rendered by the parser its options declare."""
        verbs = argparse.ArgumentParser(prog=cls.tool()).add_subparsers()
        cls.add_parser(verbs)
        return ' '.join(next(iter(verbs.choices.values())).format_usage().split()[1:])


@dataclass(frozen=True, kw_only=True)
class DirectiveVerb(CommanderVerb):
    """An action may answer named directives; unrelated work cannot clear them."""

    answers: Annotated[
        tuple[str, ...],
        Argument(
            flags=("--answers",),
            options={"nargs": "+",
                     "help": "original directive or unanswered unit message IDs this action answers"},
        ),
    ] = ()

    def named(self, ctx: Commander) -> tuple[Message, ...]:
        """The named messages still open. A name that is no longer open (answered, or out of
        the owed set since it was read) needs no answer and is dropped, never refused: the
        owed set moves while the commander writes, and refusing the whole verb over a closed
        name livelocked the gate while a chokehold held the fleet (2026-10-21 22:12). A name
        that is wrong leaves its real message owed, and the gate below refuses that."""
        return tuple(message for message in OwedAnswer.answerable(ctx.comms, self.who(ctx))
                     if message.message_id in self.answers)

    def answer_references(self, ctx: Commander) -> tuple[MessageReference, ...]:
        return tuple(message.reference for message in self.named(ctx))

    def gate(self, ctx: Commander) -> None:
        named = self.named(ctx)
        owed = tuple(rule for rule in OwedAnswer.broken(ctx.comms, self.who(ctx))
                     if not rule.answered_by(named))
        if owed:
            lines = '\n'.join(f'{rule.line()}: {rule.message.body[:80]}' for rule in owed)
            sys.exit('REFUSED, nothing recorded: messages owed an answer:\n' + lines
                     + '\nReply to the original message or use --answers MESSAGE_ID. An answer '
                     'that must wait is sent now: "waiting on X, owner Y".')
        super().gate(ctx)


class OrderVerb(CommanderVerb):
    """A verb of `order`."""


@dataclass(frozen=True, kw_only=True)
class RequestedOrderVerb(OrderVerb, DirectiveVerb, declared_name=None):
    request: Annotated[
        RequestSelection, Argument(flags=("--request",),
                                   options={"help": "owner request ID; defaults to newest open"}),
    ] = NewestOpenRequest()


@dataclass(frozen=True, kw_only=True)
class SendVerb(RequestedOrderVerb):
    help = "record the order in Core and send it"
    unit: Annotated[str, Argument(flags=("unit",))]
    body_file: Annotated[
        str, Argument(flags=("body_file",), options={"metavar": "ORDER.json"})
    ]
    clean: Annotated[
        bool,
        Argument(
            flags=("--clean",), options={"help": "the unit showed no incompetence"}
        ),
    ] = False
    reassign: Annotated[
        Reassignment,
        Argument(
            flags=("--reassign",), options={"help": "the concrete failure that forces replacement"}
        ),
    ] = NoReassignment()
    disk: Annotated[
        bool,
        Argument(
            flags=("--disk",),
            options={"help": "a cleanup order; never overrides a tool's storage rule"},
        ),
    ] = False
    traced: Annotated[
        FileLine | NoSourceReference,
        Argument(
            flags=("--traced",),
            options={
                "metavar": "PATH:LINE",
                "help": "the first failing source line, required after two owners",
            },
        ),
    ] = NoSourceReference()

    def run(self, ctx: Commander) -> None:
        ctx.send(self.unit, OrderFile.read(Path(self.body_file).read_text()), clean=self.clean,
             reassign=self.reassign, traced=self.traced,
             disk=self.disk, answers=self.answer_references(ctx), request=self.request)


@dataclass(frozen=True, kw_only=True)
class StrikeVerb(OrderVerb, DirectiveVerb):
    help = "a send-back or a false claim"
    unit: Annotated[str, Argument(flags=("unit",))]
    pid: Annotated[str, Argument(flags=("pid",), options={"help": "the struck problem"})]
    where: Annotated[
        str,
        Argument(
            flags=("where",), options={"help": "the source file:line that explains the strike"}
        ),
    ]
    why: Annotated[str, Argument(flags=("--why",))]
    caught: Annotated[
        str,
        Argument(
            flags=("--caught",),
            options={"help": '"WHO at STAGE"; stage is review/install/live/user'},
        ),
    ] = "review"

    def run(self, ctx: Commander) -> None:
        ctx.strike(self.unit, self.pid, self.why, self.caught,
                   evidence=(FileLine.from_wire(self.where),),
                   answers=self.answer_references(ctx))


@dataclass(frozen=True, kw_only=True)
class FireVerb(OrderVerb):
    help = "take every open problem off a unit; their orders and strikes stay"
    unit: Annotated[str, Argument(flags=("unit",))]
    why: Annotated[str, Argument(flags=("--why",))]

    def run(self, ctx: Commander) -> None:
        ctx.fire(self.unit, self.why)


@dataclass(frozen=True, kw_only=True)
class UnstrikeVerb(OrderVerb):
    help = "void a strike recorded in error; it stays on the record and stops counting"
    pid: Annotated[str, Argument(flags=("pid",))]
    why: Annotated[str, Argument(flags=("--why",))]
    at: Annotated[
        StrikeSelector,
        Argument(
            flags=("--at",), options={"help": "HH:MM:SS the strike was recorded, as its #comms line prints it; default the newest"}
        ),
    ] = NewestStrike()

    def run(self, ctx: Commander) -> None:
        ctx.unstrike(self.pid, self.why, self.at)


@dataclass(frozen=True, kw_only=True)
class WithdrawVerb(OrderVerb):
    help = "retire an undelivered order sent in error; its problem closes with no strike"
    pid: Annotated[str, Argument(flags=("pid",))]
    why: Annotated[str, Argument(flags=("--why",))]
    superseded_by: Annotated[
        Successor,
        Argument(
            flags=("--superseded-by",),
            options={"metavar": "PID", "help": "the problem that already owns this fix"},
        ),
    ] = NoSuccessor()

    def run(self, ctx: Commander) -> None:
        ctx.withdraw(self.pid, self.why, self.superseded_by)


@dataclass(frozen=True, kw_only=True)
class DoneVerb(OrderVerb):
    help = "Core checks the delivered commit; late delivery is a strike"
    unit: Annotated[str, Argument(flags=("unit",), options={"metavar": "UNIT"})]
    problem: Annotated[
        str,
        Argument(
            flags=("problem",),
            options={"metavar": "PROBLEM", "help": "the held problem id from order board"},
        ),
    ]
    sha: Annotated[
        CommandDeliverySource,
        Normalize(DecodedValue),
        Argument(
            flags=("--sha",),
            options={"type": CommandDeliverySource.from_text, "choices": None,
                     "help": "for a fix: commit in the recorded checkout, or op"},
        ),
    ] = dataclasses.field(default_factory=NoDeliverySource)
    screenshot: Annotated[
        str,
        Argument(
            flags=("--screenshot",),
            options={
                "metavar": "PATH",
                "help": "for an op: the screenshot the working claim stands on",
            },
        ),
    ] = ""

    seen: Annotated[str, Argument(flags=("--seen",))] = ""

    def run(self, ctx: Commander) -> None:
        todo = ctx.comms.goals.todos.resolve_order(ctx.incarnation, self.unit, self.problem)
        order = todo.order.checked()
        delivery = order.scope.delivery(
            ctx.repo_directory(order.check.directory), self.sha, self.screenshot, self.seen)
        ctx.done(todo.id, delivery)


@dataclass(frozen=True)
class PullRequestSelection(DeclaredFamily, affix="PullRequestSelection"):
    @abstractmethod
    def run(self, ctx: Commander, todo: Todo, source: Repo,
            title: str, body_file: str) -> str: ...


class NewPullRequestSelection(PullRequestSelection):
    def run(self, ctx: Commander, todo: Todo, source: Repo,
            title: str, body_file: str) -> str:
        if not title or not body_file:
            raise ValueError("Creating a PR requires --title and --body-file.")
        return todo.order.checked().open_pull_request(
            todo, source.checkout, title, Path(body_file).read_text(),
            ctx.comms.goals.todos, ctx.incarnation)


@dataclass(frozen=True)
class ExistingPullRequestSelection(PullRequestSelection):
    number: int

    @classmethod
    def from_text(cls, text: str) -> Self:
        return cls(int(text))

    def run(self, ctx: Commander, todo: Todo, source: Repo,
            title: str, body_file: str) -> str:
        if title or body_file:
            raise ValueError("--existing does not create or edit the PR; omit title and body.")
        link = PullRequestLink.existing(todo.repo, self.number)
        ctx.comms.goals.todos.apply(todo.id, RecordPullRequest(
            todo.revision, ctx.incarnation, link, todo.held().generation))
        return link.url


@dataclass(frozen=True, kw_only=True)
class PullRequestVerb(OrderVerb, declared_name="pr"):
    help = "create or adopt a PR and record its GitHub identity on this order"
    pid: Annotated[str, Argument(flags=("pid",))]
    title: Annotated[str, Argument(flags=("--title",))] = ""
    body_file: Annotated[str, Argument(flags=("--body-file",))] = ""
    existing: Annotated[
        PullRequestSelection,
        Normalize(FieldCodec.encode),
        Argument(
            flags=("--existing",),
            options={
                "type": ExistingPullRequestSelection.from_text,
                "choices": None,
                "metavar": "N",
            },
        ),
    ] = dataclasses.field(default_factory=NewPullRequestSelection)

    def run(self, ctx: Commander) -> None:
        todo = ctx.comms.goals.todos.get(self.pid)
        source = Repo.of(Path.cwd())
        holder = ctx.comms.registry.require(todo.held().owner)
        storage = ctx.comms.registry.storage_rule
        if not storage.owns_checkout(holder, source.checkout, source.checkout):
            raise ValueError(f"Run order pr from a source checkout owned by {holder.name}.")
        if not source.holds(todo):
            sys.exit(f'Refused: current checkout belongs to {source.github.to_wire()}, '
                     f'not {todo.repo.to_wire()}.')
        print(self.existing.run(ctx, todo, source, self.title, self.body_file))


@dataclass(frozen=True, kw_only=True)
class CheckVerb(OrderVerb):
    help = "replace an open order's wrong check using send's rule; keeps its review timer and strikes"
    pid: Annotated[str, Argument(flags=("pid",))]
    check: Annotated[
        str,
        Argument(
            flags=("--check",), options={"help": '"<repo> :: <shell command> -> <integer>"'}
        ),
    ]
    why: Annotated[
        str,
        Argument(
            flags=("--why",), options={"help": "what was wrong with the recorded check"}
        ),
    ]

    def run(self, ctx: Commander) -> None:
        ctx.correct_check(self.pid, self.check, self.why)


@dataclass(frozen=True, kw_only=True)
class ServeVerb(OrderVerb):
    help = "name the plan phase an open order serves; keeps its id, review timer and strikes"
    pid: Annotated[str, Argument(flags=("pid",))]
    phase: Annotated[
        str,
        Argument(
            flags=("phase",), options={"help": "one of " + ", ".join(OrderPhase.names())}
        ),
    ]

    def run(self, ctx: Commander) -> None:
        ctx.call(CommsOrderServeTool(problem=self.pid, serves=OrderPhase.member_named(self.phase)))
        print(f'{self.pid} serves {self.phase}')


@dataclass(frozen=True, kw_only=True)
class SayVerb(OrderVerb, DirectiveVerb):
    help = "a non-order message; refused if it orders work"
    units: Annotated[
        str, Argument(flags=("units",), options={"metavar": "UNIT[,UNIT]"})
    ]
    body_file: Annotated[
        str, Argument(flags=("body_file",), options={"metavar": "BODYFILE"})
    ]

    def run(self, ctx: Commander) -> None:
        ctx.say(self.units, self.body_file, self.answer_references(ctx))


@dataclass(frozen=True, kw_only=True)
class RetireVerb(OrderVerb):
    help = "stop a commander fork without discarding its open orders"

    def run(self, ctx: Commander) -> None:
        ctx.actor.retire(ctx.comms)
        print(f'{ctx.actor_name} retired')


@dataclass(frozen=True, kw_only=True)
class ProblemVerb(RequestedOrderVerb):
    help = "Core records the problem with its order, or nothing"
    order: Annotated[
        str, Argument(flags=("order",), options={"metavar": "ORDER.json"})
    ]
    to: Annotated[str, Argument(flags=("--to",))]
    where: Annotated[
        str,
        Argument(
            flags=("--where",), options={"help": "the actual file:line where this problem occurs"}
        ),
    ]
    disk: Annotated[bool, Argument(flags=("--disk",))] = False

    def run(self, ctx: Commander) -> None:
        document = OrderFile.read(Path(self.order).read_text())
        location = FileLine.from_wire(self.where)
        checkout = ctx.repo_directory(document.order.check.directory)
        if checkout.is_dir():
            ctx.comms.goals.todos.require_recurrence_declaration(
                document.order, location, checkout, document.slop, time.time())
        pid = ctx.comms.goals.todos.allocate_problem()
        ctx.send(self.to, document, new=(pid, document.defect), disk=self.disk,
                 finding=LocatedProblemFinding(location), answers=self.answer_references(ctx),
                 request=self.request)
        print(pid)


@dataclass(frozen=True, kw_only=True)
class DropVerb(OrderVerb):
    help = "drop an old order for a live newer one without a strike"
    unit: Annotated[str, Argument(flags=("unit",))]
    problem: Annotated[
        str,
        Argument(
            flags=("problem",),
            options={"metavar": "PROBLEM", "help": "the held problem id from order board"},
        ),
    ]
    by: Annotated[
        str,
        Argument(
            flags=("--by",),
            options={"metavar": "PROBLEM", "help": "the newer held problem id from order board"},
        ),
    ]

    def run(self, ctx: Commander) -> None:
        ctx.comms.goals.todos.drop(ctx.incarnation, self.unit, self.problem, self.by)
        print(f'{self.unit}: {self.problem} replaced by {self.by}')


@dataclass(frozen=True, kw_only=True)
class FoldVerb(OrderVerb):
    help = "join duplicate open problems without losing their text"
    pid: Annotated[str, Argument(flags=("pid",))]
    into: Annotated[str, Argument(flags=("--into",))]
    why: Annotated[str, Argument(flags=("--why",))]

    def run(self, ctx: Commander) -> None:
        ctx.comms.goals.todos.fold(ctx.incarnation, self.pid, self.into, self.why)
        print(f'{self.pid} folded into {self.into}')


@dataclass(frozen=True, kw_only=True)
class LinkVerb(OrderVerb):
    help = "attach an unassigned problem to an existing order"
    pid: Annotated[str, Argument(flags=("pid",))]
    unit: Annotated[str, Argument(flags=("unit",))]
    problem: Annotated[
        str,
        Argument(
            flags=("problem",),
            options={"metavar": "PROBLEM", "help": "the held problem id from order board"},
        ),
    ]

    def run(self, ctx: Commander) -> None:
        ctx.comms.goals.todos.link(ctx.incarnation, self.pid, self.unit, self.problem)
        print(f'{self.pid} held by {self.unit} through {self.problem}')


@dataclass(frozen=True, kw_only=True)
class CloseVerb(OrderVerb):
    help = "the fix works in the app; Core closes the problem"
    pid: Annotated[str, Argument(flags=("pid",))]
    works: Annotated[str, Argument(flags=("--works",))]
    artifact: Annotated[
        str,
        Argument(
            flags=("--artifact",), options={"help": "defaults to the file recorded by done --seen"}
        ),
    ] = ""

    def run(self, ctx: Commander) -> None:
        ctx.close(self.pid, self.works, self.artifact)


@dataclass(frozen=True, kw_only=True)
class HuntVerb(OrderVerb):
    help = "a hot-path read: clean unless --sent names the order it became"
    where: Annotated[
        str, Argument(flags=("where",), options={"metavar": "FILE:LINE"})
    ]
    why: Annotated[str, Argument(flags=("--why",))]
    sent: Annotated[
        str,
        Argument(
            flags=("--sent",),
            options={
                "metavar": "PROBLEM",
                "help": "the held problem id from order board; omitted means clean",
            },
        ),
    ] = ""

    def run(self, ctx: Commander) -> None:
        ctx.hunt(self.where, self.why, self.sent)


class NextHuntVerb(OrderVerb, declared_name="next-hunt"):
    help = "print the next place to hunt"

    def run(self, ctx: Commander) -> None:
        for path in HuntDue.next_hunt(ctx.comms, ctx.commander):
            print(path)


class IdleVerb(OrderVerb):
    help = "print the units Core counts idle: a recent turn and no order"

    def run(self, ctx: Commander) -> None:
        for thread in UnownedWheelSpin.idle(ctx.comms, ctx.commander):
            print(thread.name)


class BoardVerb(OrderVerb):
    help = "show Core's current Todo store without saving a board file"

    def run(self, ctx: Commander) -> None:
        print(ctx.board_text(), end='')


def main(a, ctx: Commander):
    OrderVerb.parse(a).apply(ctx)
