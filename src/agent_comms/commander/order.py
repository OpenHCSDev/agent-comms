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
import ast
import dataclasses
import re
import shlex
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, InstanceOf, PrivateAttr

from agent_comms.change_report import FileLine, MainRevision, NoSourceReference
from agent_comms.channel_targets import CommsTag
from agent_comms.declared_verb import DeclaredVerb
from agent_comms.commander.standing import UnownedWheelSpin
from agent_comms.comms import Comms, wire
from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import (
    MessageId,
    Argument,
    FieldCodec,
    MalformedRecord,
    Normalize,
    NullMember,
    SingleFieldValue,
    SubclassUnion,
    boundary_decoder,
)
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction
from agent_comms.goals import CheckCommand, CheckResult, CountCheckResult, OrderCheck
from agent_comms.maintenance_barrier import MaintenanceHeldError
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message
from agent_comms.owner_requests import NewestOpenRequest, OwnerRequestId, RequestSelection
from agent_comms.phase_evidence import MainTree, PlanReading
from agent_comms.plan_phases import OrderPhase, PlanPhase
from agent_comms.presentation import MessageNotification
from agent_comms.problem_number import ProblemNumber
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.registry_document import RegisteredCommanderProject
from agent_comms.standing_rules import HuntDue, OwedAnswer
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.held_orders import HeldOrders
from agent_comms.todos import (
    BluntText,
    CheckedDelivery,
    CleanHunt,
    DeliveryEvidence,
    DeliverySource,
    DirectiveText,
    LocatedProblemFinding,
    NoSuccessor,
    OperationDelivery,
    OrderDelivery,
    OrderedHunt,
    OrderProgress,
    OrderText,
    OrderWords,
    PendingProblemOrder,
    ProblemFinding,
    PullRequestLink,
    PullRequests,
    RecordPullRequest,
    StrikeReason,
    StrikeTodo,
    Successor,
    Todo,
    UnlocatedProblemFinding,
    VerdictAudience,
    VoidStrikeTodo,
    WithdrawTodo,
)
from agent_comms.slop import Slop
from agent_comms.tool_caller import CommanderForkCaller, NoThreadCaller
from agent_comms.order_tools import CommsChangeProblemTool, CommsFireTool, CommsOrderCheckTool, CommsOrderDoneTool, CommsOrderRunTool, CommsOrderServeTool, CommsOrderTool, CommsProblemCloseTool, CommsTodoCreateTool, CommsTodoListTool, TodoList


def refuse(text, *families):
    """Core's text families decide what the commander may write; nothing is recorded when one refuses."""
    if refused := [r for family in families if (r := family.refused(text))]:
        sys.exit('REFUSED, nothing recorded: ' + ' '.join(refused))


def struck(todo, unit):
    """Strikes count per unit and task, across every order of that task the unit held."""
    return len(todo.live_strikes(unit))


# Eliminate forwarding with inheritance and multiple parents; decompose the hierarchy if it is not sound.
# A forwarder restates an implementation pair (OOPSLA paper, Fig. 3: fields + forwarding leave routes to maintain), so
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


@dataclass(frozen=True)
class ClassNames:
    """A class's direct declarations and simple same-module parent names."""

    own: frozenset[str]
    bases: frozenset[str]

    def inherited(self, classes: Mapping[str, ClassNames], seen: frozenset[str] = frozenset()) -> frozenset[str]:
        return frozenset(name for base in self.bases if base in classes and base not in seen
                         for name in classes[base].own | classes[base].inherited(classes, seen | {base}))


class OverrideNames(ast.NodeVisitor):
    """The AST dispatches declarations; bodies and qualified bases are not direct names."""

    def __init__(self) -> None:
        self.classes: dict[str, ClassNames] = {}
        self.names: set[str] = set()

    def generic_visit(self, node: ast.AST) -> None:
        """Do not turn names nested inside an expression or body into declarations."""

    def visit_Module(self, node: ast.Module) -> None:
        for statement in node.body:
            self.visit(statement)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        body, bases = type(self)(), type(self)()
        for statement in node.body:
            body.visit(statement)
        for base in node.bases:
            bases.visit(base)
        self.classes[node.name] = ClassNames(frozenset(body.names), frozenset(bases.names))

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)

    def visit_Name(self, node: ast.Name) -> None:
        self.names.add(node.id)

    @property
    def overrides(self) -> frozenset[str]:
        return frozenset(name for declaration in self.classes.values()
                         for name in declaration.own & declaration.inherited(self.classes))


def overrides(sources):
    """Names answered again for a question a same-module base declares."""
    found = set()
    for text in sources:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        declarations = OverrideNames()
        declarations.visit(tree)
        found.update(declarations.overrides)
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
    """What `order done` checks: the order's merged PR, or an operation the commander names."""


@dataclass(frozen=True)
class PullRequestDeliverySource(CommandDeliverySource):
    """Core reads the order's merged PR: its merge commit is checked, its first commit recorded."""

    def require_report(self) -> None:
        pass

    def delivery(self, todo: Todo, repo: Path, evidence: DeliveryEvidence) -> CheckedDelivery:
        delivery = PullRequests((todo,)).merged_delivery(todo, repo, evidence)
        diff = Repo.git(repo, 'show', '--first-parent', '-m', '--format=', delivery.commit)
        changed = re.findall(r'^\+\+\+ b/(\S+\.py)$', diff, re.M)
        sources = [Repo.git(repo, 'show', f'{delivery.commit}:{path}') for path in changed]
        if added := forwarders_added(diff, sources):
            raise ValueError(f'Refused: pull request merge {delivery.commit} adds forwarders '
                             f'{added[:5]}; slop never counts as delivery. Order the hierarchy fix.')
        return delivery


@dataclass(frozen=True)
class OpDeliverySource(CommandDeliverySource):
    def delivery(self, todo: Todo, repo: Path, evidence: DeliveryEvidence) -> CheckedDelivery:
        return OperationDelivery(evidence=evidence)


@dataclass(frozen=True)
class CheckRequest(CheckCommand):
    """A text check selects a configured repository before becoming a stored path."""

    repository: str
    command: str
    result: CheckResult
    LINE: ClassVar[re.Pattern] = re.compile(r"(\S*)\s*::\s*(.+?)\s*->\s*(\d+)", re.DOTALL)

    @classmethod
    def parse(cls, line: str) -> CheckRequest | None:
        """Read `<repo> :: <shell command> -> <integer>`; else None."""
        found = cls.LINE.fullmatch(line.strip())
        if not found:
            return None
        request = cls(found[1], found[2], CountCheckResult(int(found[3])))
        request.require_writable()
        return request

    def resolve(self, repos: tuple[Repo, ...]) -> OrderCheck:
        return OrderCheck(Path(self.repository), self.command, self.result).registered(repos)


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
            document = FieldCodec.decode(cls, FieldCodec.parse(text))
        except MalformedRecord as error:
            sys.exit(f'Refused order, nothing recorded:\n{error}')
        created = tuple(line for slop in document.slop for line in slop.by.owed())
        return replace(document, order=replace(document.order, created=created))

    @property
    def prose(self) -> str:
        reason = f'\nUnplanned reason: {self.unplanned_reason}' if self.unplanned_reason else ''
        return f'{self.defect}\n{self.why_stupid}{reason}'

    def problems_found(self, clean: bool) -> list[str]:
        wording: tuple[type[OrderWords], ...] = (*DirectiveText.checks(), OrderText)
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

    def resolved(self, finding: ProblemFinding) -> OrderFile:
        return replace(self, order=replace(self.order, finding=finding))

    def registered(self, repos: tuple[Repo, ...]) -> OrderFile:
        return replace(self, order=replace(self.order, check=self.order.check.registered(repos)))
STAGES = (
    "tool call",
    "review",
    "install",
    "live",
    "user",
)  # where a mistake was caught; later is worse


class CommanderContext(BaseModel):
    """The selected root's bus and canonical reads, shared by command families."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    root: Path

    actor: Annotated[
        InstanceOf[CommanderForkCaller] | InstanceOf[NoThreadCaller], Field(exclude=True)
    ] = NoThreadCaller()
    _comms: Comms = PrivateAttr()

    def model_post_init(self, context: object) -> None:
        self._comms = wire(self.root)

    @classmethod
    def read_root(cls, root: Path | str) -> Self:
        return cls(root=Path(root).resolve())

    @classmethod
    def on_root(cls, commander: str, repos: tuple[Repo, ...], root: Path) -> Self:
        context = cls.read_root(root)
        thread = context.comms.registry.require(commander)
        context.comms.registry.set_commander_project(
            RegisteredCommanderProject(thread.incarnation, repos)
        )
        return context

    @property
    def commander(self) -> str:
        snapshot = self.comms.registry.snapshot()
        project = snapshot.commander_project.require()
        (thread,) = project.commanders(snapshot)
        return thread.name

    @property
    def repos(self) -> tuple[Repo, ...]:
        return self.comms.registry.snapshot().commander_project.require().repos

    def order_file(self, path: str) -> OrderFile:
        """The order file with its check bound to a registered checkout, once, where it enters."""
        document = OrderFile.read(Path(path).read_text())
        try:
            return document.registered(self.repos)
        except ValueError as error:
            sys.exit(f'Refused order, nothing recorded:\n{error}')

    def plan_reading(self, todos: tuple[Todo, ...]) -> PlanReading:
        repository = Repo.declaring(self.repos, PlanPhase.__module__)
        return PlanReading(todos, MainTree.of(repository))

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
             answers: tuple[MessageReference, ...] = ()) -> MessageId:
        return self.published(audience, body, answers).message_id

    def published(self, audience: VerdictAudience, body: str,
                  answers: tuple[MessageReference, ...] = ()) -> Message:
        target = audience.target
        return self.comms.messaging.send(self.actor_name, target.name,
            target.addressed(audience.threads, body), answers=answers).message

    def delivery(self, reference: MessageReference) -> str:
        """The recipients' delivery as the notification owner reports it.

        A publish alone is only queued."""
        outcomes = MessageNotification.references(
            self.comms.root, self.comms.registry, self.comms.bus.bus, (reference,))
        return MessageNotification.summary(outcomes.get((reference.seq, reference.message_id), ()))

    def call(self, tool):
        return self.actor.as_thread(self.commander).invoke(tool, self.comms)

    def todos(self):
        return self.comms.goals.todos.for_commander(self.incarnation)

    def read_check(self, line: str) -> OrderCheck:
        if not (request := CheckRequest.parse(line)):
            sys.exit('Refused: Check reads "<repo> :: <shell command> -> <integer>"')
        return request.resolve(self.repos)

    def held_orders(self) -> tuple[Todo, ...]:
        return tuple(todo for todo in self.todos() if todo.holder)

    def held_by(self, unit: str) -> tuple[Todo, ...]:
        return self.comms.goals.todos.held_by(
            self.incarnation, self.comms.registry.require(unit).incarnation)

    def units(self) -> frozenset[str]:
        """The current registry names of every unit this commander ordered."""
        snapshot = self.comms.registry.snapshot()
        return frozenset(thread.name for unit in self.comms.goals.todos.units(self.incarnation)
                         for thread in snapshot.threads_for(unit.require_recorded()))

    def progress(self) -> dict[str, OrderProgress]:
        listed: TodoList = self.call(CommsTodoListTool(thread=self.commander))
        return listed.progress

    def importers(self, modules: set[str], check: OrderCheck) -> list[str]:
        """The other configured repos whose `from agent_comms` imports name a module, unless the
        check reads them."""
        pattern = rf'^\s*(from|import) agent_comms\b.*\b({"|".join(sorted(modules))})\b'
        return [str(repo.checkout) for repo in self.repos
                if repo.checkout != check.directory.resolve()
                and str(repo.checkout) not in check.command
                and subprocess.run(['grep', '-rqE', '--include=*.py', pattern,
                                    str(repo.checkout / repo.source)]).returncode == 0]

    def last_activity(self, unit):
        thread = self.comms.registry.require(unit)
        return thread.runtime.history_source(self.comms.root, thread.incarnation).last_tool_call()



class OrderedProblems(ABC):
    """The problems one sent order covers: a problem born with it, or open ones it names."""

    @abstractmethod
    def group(self, document: OrderFile) -> list[str]: ...

    @abstractmethod
    def refusals(self, document: OrderFile, group: Sequence[str],
                 listed: Mapping[str, Todo]) -> list[str]: ...

    @abstractmethod
    def replacing(self, unit: ThreadIncarnation, group: Sequence[str],
                  listed: Mapping[str, Todo]) -> bool:
        """Whether this order replaces the unit's own earlier order on the same problems."""

    def carried(self, order: PendingProblemOrder, group: Sequence[str],
                listed: Mapping[str, Todo]) -> PendingProblemOrder:
        """The order with the pull requests its problems already hold; a new problem holds none."""
        return order

    @abstractmethod
    def record(self, context: CommanderContext, order: PendingProblemOrder, unit: str,
               group: Sequence[str], traced: FileLine | NoSourceReference, request: OwnerRequestId,
               review_at: RecordedEventTime) -> None: ...


@dataclass(frozen=True, slots=True)
class NewProblem(OrderedProblems):
    """Core records the problem with its first order in one step."""

    problem: str
    text: str

    def group(self, document: OrderFile) -> list[str]:
        return [self.problem]

    def refusals(self, document: OrderFile, group: Sequence[str],
                 listed: Mapping[str, Todo]) -> list[str]:
        return ['problems: fix: omit IDs when creating a new problem'] if document.problems else []

    def replacing(self, unit: ThreadIncarnation, group: Sequence[str],
                  listed: Mapping[str, Todo]) -> bool:
        return False

    def record(self, context: CommanderContext, order: PendingProblemOrder, unit: str,
               group: Sequence[str], traced: FileLine | NoSourceReference, request: OwnerRequestId,
               review_at: RecordedEventTime) -> None:
        context.call(CommsTodoCreateTool(owner=unit, order=CreateProblemGoalAction(
            problem=self.problem, repo=Repo.of(order.check.directory).github,
            text=self.text, order=order, request=request, review_at=review_at,
            commander=context.commander, assigner=context.actor_identity)))


@dataclass(frozen=True, slots=True)
class ListedProblems(OrderedProblems):
    """The open problems the order document lists."""

    def group(self, document: OrderFile) -> list[str]:
        return list(document.problems)

    def refusals(self, document: OrderFile, group: Sequence[str],
                 listed: Mapping[str, Todo]) -> list[str]:
        missing = [pid for pid in group if pid not in listed]
        return [f'{missing} not open problems; fix: name existing open problem IDs'] if missing else []

    def replacing(self, unit: ThreadIncarnation, group: Sequence[str],
                  listed: Mapping[str, Todo]) -> bool:
        return bool(group) and all(
            pid in listed and any(assignment.owner_identity == unit for assignment
                                  in listed[pid].holder_state.current_assignments())
            for pid in group)

    def carried(self, order: PendingProblemOrder, group: Sequence[str],
                listed: Mapping[str, Todo]) -> PendingProblemOrder:
        """A replacement order keeps the problem's pull requests: a restore cites their heads."""
        return order.carrying(link for pid in group if pid in listed
                              for link in listed[pid].order.linked_requests)

    def record(self, context: CommanderContext, order: PendingProblemOrder, unit: str,
               group: Sequence[str], traced: FileLine | NoSourceReference, request: OwnerRequestId,
               review_at: RecordedEventTime) -> None:
        for pid in group:
            context.call(CommsOrderTool(owner=unit, order=OrderGoalAction(
                problem=pid, expected_revision=context.comms.goals.todos.get(pid).revision,
                assigner=context.actor_identity, traced=traced, order=order, request=request,
                review_at=review_at, commander=context.commander)))


class Reassignment(SubclassUnion, affix="Reassignment"):
    """Why a held problem changes owner, or no change of owner."""

    def send_back(self, context: Commander, document: OrderFile, unit: str,
                  problem: str, struck_already: bool) -> None:
        if struck_already:
            print(f'{unit} already struck on this order of {problem}; '
                  'the send-back counts that one miss once')
        else:
            self.record_strike(context, document, unit, problem)

    @abstractmethod
    def record_strike(self, context: Commander, document: OrderFile,
                      unit: str, problem: str) -> None: ...

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

    def record_strike(self, context: Commander, document: OrderFile,
                      unit: str, problem: str) -> None:
        """Taking over work does not charge a mistake."""

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
    def record_strike(self, context: Commander, document: OrderFile,
                      unit: str, problem: str) -> None:
        context.strike(unit, problem, f'sent back on {problem}: {document.prose.strip()}',
                       evidence=(document.order.owner,))

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
    def send(self, unit: str, document: OrderFile, clean: bool = False,
             reassign: Reassignment = NoReassignment(), target: OrderedProblems = ListedProblems(),
             traced: FileLine | NoSourceReference = NoSourceReference(),
             disk: bool = False, finding: ProblemFinding = UnlocatedProblemFinding(),
             answers: tuple[MessageReference, ...] = (), *, request: RequestSelection) -> None:
        """Record the decoded order, or report every failed prerequisite before any write."""
        document = document.resolved(finding)
        document.order.send(self, document, unit, clean, reassign, target, traced, answers, request)

    def say(self, units: str, body_file: str,
            answers: tuple[MessageReference, ...] = ()) -> None:
        body = open(body_file).read()
        refuse(body, *DirectiveText.checks())
        try:
            audience = VerdictAudience(tuple(units.split(',')))
            message = self.published(audience, body.rstrip(), answers)
            print(message.message_id, self.delivery(message.reference))
        except MaintenanceHeldError as held:
            # A say records nothing but its post, so the whole command is the resend.
            sys.exit(f'Refused, nothing sent: {held}\nAfter the install reports finished, '
                     'resend:\n  ' + shlex.join(('agent-comms-commander', *sys.argv[1:])))

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
        if todo.held().owner != unit:
            sys.exit(f'Refused: {unit} does not hold {task}; strike the unit that holds it.')
        self.recorded(unit, todo, kind(todo.revision, self.actor_identity, todo.held(),
                                      f'{why} (caught by {caught})',
                                      evidence=evidence), answers)

    def recorded(self, unit: str, todo: Todo, strike: StrikeTodo,
                 answers: tuple[MessageReference, ...] = ()) -> None:
        """Core records the strike on the task's Todo, then posts it to #comms."""
        if (self.call(CommsChangeProblemTool(problem=todo.id, operation=strike))
                .todo.revision == todo.revision):
            return
        self.posted(unit, self.comms.goals.todos.get(todo.id), strike, answers)

    def posted(self, unit: str, todo: Todo, strike: StrikeTodo,
               answers: tuple[MessageReference, ...] = ()) -> None:
        """Publish a strike Core already recorded on the task's Todo to #comms."""
        n = struck(todo, unit)
        taken = f", {todo.id} taken away" if todo.holder != unit else ""
        line = (f'{time.strftime("%Y-%m-%d")} {strike.at.clock()} {unit} (strike {n} on {todo.id}'
                f'{taken}): {strike.reason}')
        self.on_record(line, 'Strike', VerdictAudience.of(unit, todo), answers)
        print(f'{unit}: {n} strike(s) on {todo.id}{taken}')

    def hunt(self, where, why, sent, repository=""):
        """Record the commander's source read, whether or not it became an order.
        FILE:LINE is read in the registered repository's checkout, never the shell's cwd."""
        ref = FileLine.from_wire(where)
        try:
            directory = Repo.selected(self.repos, repository).checkout
        except ValueError as error:
            sys.exit(f'Refused: {error}')
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
        # posted to #comms, so refused before Core records the void
        refuse(why, *DirectiveText.checks())
        self.call(CommsChangeProblemTool(problem=task, operation=VoidStrikeTodo(
            todo.revision, self.incarnation, voided, why)))
        unit = voided.previous.owner
        after = self.comms.goals.todos.get(task)
        n = struck(after, unit)
        line = (f'{time.strftime("%Y-%m-%d %H:%M")} {unit} (strike voided on {task}, '
                f'recorded {voided.at.clock()}; {n} strike(s) remain; held by '
                f'{after.holder or "nobody"}): {why}')
        self.on_record(line, 'Void', VerdictAudience.of(unit, todo))
        print(f'{unit}: {n} strike(s) on {task}; held by {after.holder or "nobody"}')

    def withdraw(self, pid: str, why: str, superseded_by: Successor = NoSuccessor()) -> None:
        """Core retires an undelivered order sent in error.

        The reason goes on the public record and to the unit that held it."""
        todo = self.comms.goals.todos.get(pid)
        todo.order.require_withdrawal()
        # posted to #comms and the holder, so refused before Core records it
        refuse(why, *DirectiveText.checks())
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
class CommanderVerb(DeclaredVerb, affix="Verb"):
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
        ctx.comms.registry.require(ctx.commander).require_commander()
        self.fork.commanding(ctx.comms, ctx.commander)
        ctx = ctx.model_copy(update={'actor': self.fork})
        self.gate(ctx)
        self.run(ctx)

    def gate(self, ctx: Commander) -> None:
        ctx.comms.registry.require(self.who(ctx)).require_commander()

    def resolves_claim(self, unit: str, problem: str) -> bool:
        """Ordinary messages and actions do not judge a delivery claim."""
        return False

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
        tuple[MessageId, ...],
        Argument(
            flags=("--answers",),
            options={"nargs": "+",
                     "help": "original directive or unanswered unit message IDs this action answers"},
        ),
    ] = ()

    def run(self, ctx: Commander) -> None:
        """Resolve names and uncovered debt once, then pass the exact references to the action.

        Closed or unknown names are dropped: a stale name must not livelock an otherwise
        valid answer. A wrong name still leaves its real debt uncovered and refuses the action.
        """
        now = time.time()
        opened = OwedAnswer.opened(ctx.comms, self.who(ctx))
        snapshot = ctx.comms.registry.snapshot()
        reached = frozenset(snapshot.canonical_name(name) for name in self.addressed)
        named = (tuple(rule.message for rule in opened
                       if rule.message.message_id in self.answers)
                 if self.answers else self.unnamed_answers(opened, reached))
        misaddressed = tuple(reason for rule in opened
                             if (reason := rule.misaddressed(named, reached)))
        if misaddressed:
            sys.exit('REFUSED, nothing recorded: --answers names a message this action never'
                     ' reaches:\n' + '\n'.join(misaddressed))
        owed = tuple(rule for rule in opened if rule.due(now) and not rule.answered_by(named))
        blocked = []
        for rule in owed:
            if rule.blocks(reached, self):
                blocked.append(rule)
            else:
                print(f'WARNING: {rule.line()}')
        if blocked:
            lines = '\n'.join(
                f'{rule.line()}: {rule.message.body[:80]}\n{rule.clearing_action()}'
                for rule in blocked
            )
            sys.exit('REFUSED, nothing recorded: messages owed an answer:\n' + lines)
        self.respond(ctx, tuple(dict.fromkeys(message.reference for message in named)))

    def unnamed_answers(self, opened: tuple[OwedAnswer, ...],
                        reached: frozenset[str]) -> tuple[Message, ...]:
        """What this action answers when --answers names nothing: only what it names."""
        return ()

    @property
    @abstractmethod
    def addressed(self) -> tuple[str, ...]:
        """The threads this action's message is for; only their asks can it answer."""

    @abstractmethod
    def respond(self, ctx: Commander, references: tuple[MessageReference, ...]) -> None: ...


class OrderVerb(CommanderVerb):
    """A verb of `order`."""


@dataclass(frozen=True, kw_only=True)
class Reasoned:
    """A verb that records why the commander acted."""

    why: Annotated[str, Argument(flags=("--why",))]


@dataclass(frozen=True, kw_only=True)
class RequestedOrderVerb(OrderVerb, DirectiveVerb, declared_name=None):
    request: Annotated[
        RequestSelection, Argument(flags=("--request",),
                                   options={"help": "owner request ID; defaults to newest open"}),
    ] = NewestOpenRequest()
    clean: Annotated[
        bool,
        Argument(
            flags=("--clean",), options={"help": "the unit showed no incompetence"}
        ),
    ] = False
    disk: Annotated[
        bool,
        Argument(
            flags=("--disk",),
            options={"help": "a cleanup order; never overrides a tool's storage rule"},
        ),
    ] = False


@dataclass(frozen=True, kw_only=True)
class SendVerb(RequestedOrderVerb):
    help = "record the order in Core and send it"
    unit: Annotated[str, Argument(flags=("unit",))]
    body_file: Annotated[
        str, Argument(flags=("body_file",), options={"metavar": "ORDER.json"})
    ]
    reassign: Annotated[
        Reassignment,
        Argument(
            flags=("--reassign",), options={"help": "the concrete failure that forces replacement"}
        ),
    ] = NoReassignment()
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

    @property
    def addressed(self) -> tuple[str, ...]:
        return (self.unit,)

    def resolves_claim(self, unit: str, problem: str) -> bool:
        return (self.unit == unit
                and problem in OrderFile.read(Path(self.body_file).read_text()).problems)

    def respond(self, ctx: Commander, references: tuple[MessageReference, ...]) -> None:
        ctx.send(self.unit, ctx.order_file(self.body_file), clean=self.clean,
             reassign=self.reassign, traced=self.traced,
             disk=self.disk, answers=references, request=self.request)


@dataclass(frozen=True, kw_only=True)
class StrikeVerb(Reasoned, OrderVerb, DirectiveVerb):
    help = "a send-back or a false claim"
    unit: Annotated[str, Argument(flags=("unit",))]
    pid: Annotated[str, Argument(flags=("pid",), options={"help": "the struck problem"})]
    where: Annotated[
        str,
        Argument(
            flags=("where",), options={"help": "the source file:line that explains the strike"}
        ),
    ]
    caught: Annotated[
        str,
        Argument(
            flags=("--caught",),
            options={"help": '"WHO at STAGE"; stage is review/install/live/user'},
        ),
    ] = "review"

    @property
    def addressed(self) -> tuple[str, ...]:
        return (self.unit,)

    def respond(self, ctx: Commander, references: tuple[MessageReference, ...]) -> None:
        ctx.strike(self.unit, self.pid, self.why, self.caught,
                   evidence=(FileLine.from_wire(self.where),),
                   answers=references)


@dataclass(frozen=True, kw_only=True)
class FireVerb(Reasoned, OrderVerb):
    help = "take every open problem off a unit; their orders and strikes stay"
    unit: Annotated[str, Argument(flags=("unit",))]

    def run(self, ctx: Commander) -> None:
        ctx.fire(self.unit, self.why)


@dataclass(frozen=True, kw_only=True)
class UnstrikeVerb(Reasoned, OrderVerb):
    help = "void a strike recorded in error; it stays on the record and stops counting"
    pid: Annotated[str, Argument(flags=("pid",))]
    at: Annotated[
        StrikeSelector,
        Argument(
            flags=("--at",), options={"help": "HH:MM:SS the strike was recorded, as its #comms line prints it; default the newest"}
        ),
    ] = NewestStrike()

    def run(self, ctx: Commander) -> None:
        ctx.unstrike(self.pid, self.why, self.at)


@dataclass(frozen=True, kw_only=True)
class WithdrawVerb(Reasoned, OrderVerb):
    help = "retire an undelivered order sent in error; its problem closes with no strike"
    pid: Annotated[str, Argument(flags=("pid",))]
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
    help = "Core checks the order's merged PR at its merge commit; late delivery is a strike"
    unit: Annotated[str, Argument(flags=("unit",), options={"metavar": "UNIT"})]
    problem: Annotated[
        str,
        Argument(
            flags=("problem",),
            options={"metavar": "PROBLEM", "help": "the held problem id from order board"},
        ),
    ]
    source: Annotated[
        CommandDeliverySource,
        Normalize(FieldCodec.encode),
        Argument(
            flags=("--op",),
            options={"action": "store_const", "const": OpDeliverySource(),
                     "help": "an operation checked on fresh main, not the order's merged PR"},
        ),
    ] = dataclasses.field(default_factory=PullRequestDeliverySource)
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

    def resolves_claim(self, unit: str, problem: str) -> bool:
        return self.unit == unit and self.problem == problem

    def run(self, ctx: Commander) -> None:
        todo = ctx.comms.goals.todos.resolve_order(ctx.incarnation, self.unit, self.problem)
        order = todo.order.checked()
        delivery = order.scope.delivery(
            todo, order.check.directory, self.source,
            self.screenshot, self.seen)
        ctx.call(CommsOrderDoneTool(
            problem=todo.id, order_id=order.id, delivery=delivery,
            claims=delivery.done_claims(order)))
        print(f'{todo.held().owner}: order {order.id} delivered as {delivery.work}')


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
        if not storage.owns_checkout(holder, source.checkout, source.github):
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
        check = ctx.read_check(self.check)
        ctx.call(CommsOrderCheckTool(problem=self.pid, check=check, why=self.why))
        print(f'{self.pid}: order now checks `{check.command}`')


@dataclass(frozen=True, kw_only=True)
class RunVerb(OrderVerb):
    help = "replace an open application order's wrong run text; keeps its review timer and strikes"
    pid: Annotated[str, Argument(flags=("pid",))]
    text: Annotated[
        str, Argument(flags=("--run",), options={"help": "what the installed app must show"})
    ]
    why: Annotated[
        str, Argument(flags=("--why",), options={"help": "what was wrong with the recorded run"})
    ]

    def run(self, ctx: Commander) -> None:
        ctx.call(CommsOrderRunTool(problem=self.pid, run=self.text, why=self.why))
        print(f'{self.pid}: order now works when {self.text}')


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

    @property
    def addressed(self) -> tuple[str, ...]:
        return VerdictAudience(tuple(self.units.split(','))).threads

    def unnamed_answers(self, opened: tuple[OwedAnswer, ...],
                        reached: frozenset[str]) -> tuple[Message, ...]:
        """A reply answers every open ask from the threads it reaches."""
        asked = tuple(rule.message for rule in opened if rule.asked_by(reached))
        if asked:
            print('answers: ' + ' '.join(message.message_id for message in asked))
        return asked

    def respond(self, ctx: Commander, references: tuple[MessageReference, ...]) -> None:
        ctx.say(self.units, self.body_file, references)


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
    @property
    def addressed(self) -> tuple[str, ...]:
        return (self.to,)

    def respond(self, ctx: Commander, references: tuple[MessageReference, ...]) -> None:
        document = ctx.order_file(self.order)
        location = FileLine.from_wire(self.where)
        checkout = document.order.check.directory
        if checkout.is_dir():
            ctx.comms.goals.todos.require_recurrence_declaration(
                document.order, location, checkout, document.slop, time.time())
        pid = ctx.comms.goals.todos.allocate_problem()
        ctx.send(self.to, document, clean=self.clean,
                 target=NewProblem(pid, document.defect), disk=self.disk,
                 finding=LocatedProblemFinding(location), answers=references,
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
class FoldVerb(Reasoned, OrderVerb):
    help = "join duplicate open problems without losing their text"
    pid: Annotated[str, Argument(flags=("pid",))]
    into: Annotated[str, Argument(flags=("--into",))]

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
class HuntVerb(Reasoned, OrderVerb):
    help = "a hot-path read: clean unless --sent names the order it became"
    where: Annotated[
        str, Argument(flags=("where",), options={"metavar": "FILE:LINE"})
    ]
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
    repository: Annotated[
        str,
        Argument(flags=("--repo",), options={
            "help": "registered repository FILE:LINE is in; required when several are registered"}),
    ] = ""

    def run(self, ctx: Commander) -> None:
        ctx.hunt(self.where, self.why, self.sent, self.repository)


class NextHuntVerb(OrderVerb, declared_name="next-hunt"):
    help = "print the next place to hunt"

    def run(self, ctx: Commander) -> None:
        for path in HuntDue.next_hunt(ctx.comms, ctx.commander,
                                      HeldOrders.read(ctx.comms, ctx.commander).todos):
            print(path)


class IdleVerb(OrderVerb):
    help = "print the units Core counts idle: a recent turn and no order"

    def run(self, ctx: Commander) -> None:
        scan = HeldOrders.read(ctx.comms, ctx.commander)
        for thread in UnownedWheelSpin.idle(ctx.comms, ctx.commander, scan):
            print(thread.name)




def main(a, ctx: Commander):
    OrderVerb.parse(a).apply(ctx)
