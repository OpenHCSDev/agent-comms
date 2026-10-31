"""What proves a plan phase done: facts on its repository's fetched ``origin/main``, and for
the merge gate the repository's own GitHub rules.

Task acceptance never proves a phase. Each phase declares the evidence its plan's
"Done means" names, and ``PlanPhase.done`` reads only that evidence. Evidence that reads
only main is retained by the heartbeat with that commit; a pass with unmoved main
reuses those answers. Forge evidence declares its independent per-pass lifetime.
"""

from __future__ import annotations

import ast
import copy
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path, PurePosixPath
from threading import Lock
from subprocess import TimeoutExpired
from typing import TYPE_CHECKING, ClassVar, get_args, get_origin

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.head_review import MergeHeadReview
from agent_comms.repo import Checkout, CheckoutHistory, GitCommandError, Repo

if TYPE_CHECKING:
    from agent_comms.todos import Todo


@dataclass(frozen=True)
class MainModule:
    """One Python module of main, parsed once per blob."""

    file_path: str
    source: str
    module: ast.Module


class Census(ABC):
    """The installed NominalRefactorAdvisor's census, as evidence counts with it. Its
    implementation owns the advisor, so declaring evidence never loads the advisor; only a
    tree read for a plan carries one."""

    @abstractmethod
    def god_function(self, module: MainModule,
                     function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool: ...

    @abstractmethod
    def absence_probes(self, node: ast.AST) -> int: ...

    @abstractmethod
    def audit_package(self) -> Path: ...


@dataclass(frozen=True)
class MainTree:
    """One commit of a repository's fetched ``origin/main``, counted with ``census``; evidence
    never reads a worktree."""

    repo: Repo
    commit: str
    census: Census = field(compare=False, repr=False)
    history: CheckoutHistory = field(compare=False, repr=False, kw_only=True)
    branch: ClassVar[str] = "main"
    source: ClassVar[str] = ".py"
    parsed: dict[str, tuple[str, MainModule]] = field(
        default_factory=dict, compare=False, repr=False, kw_only=True,
    )

    def __post_init__(self) -> None:
        self.history.include(self.commit)

    @classmethod
    def of(cls, repo: Repo, census: Census) -> MainTree:
        return cls.from_history(repo, census, Checkout(repo.checkout).read())

    @classmethod
    def from_history(cls, repo: Repo, census: Census, history: CheckoutHistory) -> MainTree:
        return cls(repo, history.resolve(f"{repo.remote}/{cls.branch}"), census, history=history)

    def has(self, path: str) -> bool:
        return not self.missing((path,))[0]

    @cached_property
    def objects(self) -> dict[str, str]:
        """This tree's membership, including directories, without requiring blob contents."""
        return Repo.tree_objects(self.repo.checkout, self.commit, (), "-r", "-t")

    @cached_property
    def files(self) -> dict[str, str]:
        """This tree's blobs, listed once per commit: every module read filters this listing."""
        return Repo.tree_objects(self.repo.checkout, self.commit, (), "-r")

    def missing(self, paths: tuple[str, ...]) -> tuple[bool, ...]:
        """Tree membership does not require a present path's promised contents."""
        return tuple(path not in self.objects for path in paths)

    def grep(self, *options: str, pattern: str, paths: tuple[str, ...]) -> list[str]:
        """``git grep -E`` rows of this tree's Python modules, like ``module`` and ``modules``;
        the Markdown beside them quotes code it does not hold.

        A submodule is another repository's tree, regardless of local recurse settings.
        Git pathspecs such as ``:!file`` exclude.
        """
        return [row for row in self.text_grep(*options, pattern=pattern, paths=paths)
                if row.split(":", 2)[1].endswith(self.source)]

    def text_grep(self, *options: str, pattern: str, paths: tuple[str, ...]) -> tuple[str, ...]:
        """Declared text evidence, including documents, from this exact repository tree."""
        return Repo.grep(
            self.repo.checkout,
            "--no-recurse-submodules",
            *options,
            "-E",
            "-e",
            pattern,
            self.commit,
            "--",
            *paths,
        )

    def count(self, pattern: str, paths: tuple[str, ...]) -> int:
        """Matching lines under ``paths``."""
        return sum(
            int(row.rsplit(":", 1)[1]) for row in self.grep("-c", pattern=pattern, paths=paths)
        )

    def matching_lines(self, pattern: str, paths: tuple[str, ...]) -> frozenset[tuple[str, int]]:
        """Each ``(path, line)`` under ``paths`` whose line matches."""
        return frozenset(
            (path, int(line))
            for _, path, line, _ in (
                row.split(":", 3) for row in self.grep("-n", pattern=pattern, paths=paths)
            )
        )

    def module(self, path: str) -> MainModule:
        module, = self.modules((path,))
        return module

    def present_modules(self, paths: tuple[str, ...]) -> Iterator[MainModule]:
        """The parsed modules of the ``paths`` main holds. Plans finish by deleting files, so
        a declared path main no longer holds has no module, and nothing it held is left."""
        missing = self.missing(paths)
        for path, absent in zip(paths, missing, strict=True):
            if not absent:
                yield self.module(path)

    def modules(self, paths: tuple[str, ...]) -> Iterator[MainModule]:
        """Every Python module under ``paths``. Each path keeps its last parsed blob, so every
        evidence reading one commit parses it once, and the rest are read in one
        ``git cat-file --batch``."""
        for deleted in self.parsed.keys() - self.objects.keys():
            del self.parsed[deleted]
        roots = tuple(path.rstrip("/") for path in paths)
        blobs = [(blob, name) for name, blob in self.files.items() if name.endswith(self.source)
                 and any(name == root or name.startswith(f"{root}/") for root in roots)]
        fresh = [(blob, name) for blob, name in blobs
                 if self.parsed.get(name, ("", None))[0] != blob]
        contents = Repo.blobs(self.repo.checkout, *(blob for blob, _ in fresh))
        for (blob, name), content in zip(fresh, contents, strict=True):
            source = content.decode()
            self.parsed[name] = blob, MainModule(name, source, ast.parse(source, filename=name))
        yield from (self.parsed[name][1] for _, name in blobs)

    def contains(self, sha: str) -> bool:
        return self.history.contains(sha, self.commit)


class UnreadableEvidenceError(Exception):
    """The evidence's source could not be read this pass; the phase is unknown, not failed."""


class EvidenceRead(ABC):
    """What one reading learned of one evidence."""

    @abstractmethod
    def held(self) -> bool: ...

    @abstractmethod
    def describe(self, evidence: PhaseEvidence) -> str: ...

    def retained(self, evidence: PhaseEvidence, previous: MainTree, current: MainTree) -> bool:
        return evidence.retained(previous, current)


@dataclass(frozen=True)
class EvidenceFound(EvidenceRead):
    holds: bool

    def held(self) -> bool:
        return self.holds

    def describe(self, evidence: PhaseEvidence) -> str:
        return evidence.describe()


@dataclass(frozen=True)
class EvidenceUnread(EvidenceRead):
    """The source could not be read: not done, and the description says why."""

    reason: str

    def held(self) -> bool:
        return False

    def describe(self, evidence: PhaseEvidence) -> str:
        return f"unknown ({self.reason}): {evidence.describe()}"

    def retained(self, evidence: PhaseEvidence, previous: MainTree, current: MainTree) -> bool:
        """A source recovering from failure does not require a new main commit."""
        return False


@dataclass(frozen=True)
class PlanReading:
    """One look at the plans: the order history and the plan repository's main.

    Each evidence is read once per reading, however many phases wait on it."""

    todos: tuple[Todo, ...]
    main: MainTree
    reads: dict[PhaseEvidence, EvidenceRead] = field(
        default_factory=dict, compare=False, repr=False
    )

    @contextmanager
    def source(self, evidence: PhaseEvidence) -> Iterator[None]:
        """One failure boundary for acquiring and observing the evidence's source."""
        try:
            yield
        except (GitCommandError, OSError, TimeoutExpired, UnreadableEvidenceError) as error:
            self.reads[evidence] = EvidenceUnread(str(error))

    def prepare(self, evidence: PhaseEvidence) -> None:
        if evidence not in self.reads:
            with self.source(evidence):
                evidence.prepare(self)

    def read(self, evidence: PhaseEvidence) -> EvidenceRead:
        if evidence not in self.reads:
            with self.source(evidence):
                self.reads[evidence] = EvidenceFound(evidence.holds(self))
        return self.reads[evidence]

    def following(self, todos: tuple[Todo, ...], main: MainTree) -> PlanReading:
        main = replace(main, parsed=self.main.parsed)
        return type(self)(todos, main, {
            evidence: answer for evidence, answer in self.reads.items()
            if answer.retained(evidence, self.main, main)
        })

    def holds(self, evidence: PhaseEvidence) -> bool:
        return self.read(evidence).held()

    def describe(self, evidence: PhaseEvidence) -> str:
        return self.read(evidence).describe(evidence)


class PlanReadings:
    """A plan read's lifetime; ordinary commands take one explicit snapshot. The member that
    reads plans declares the census its trees are counted with."""

    census: ClassVar[Census]

    def observed(self, repo: Repo, todos: tuple[Todo, ...], history: CheckoutHistory) -> PlanReading:
        return PlanReading(todos, MainTree.from_history(repo, self.census, history))

    def prepare_reading(self, repo: Repo, todos: tuple[Todo, ...],
                evidence: tuple[PhaseEvidence, ...], *, history: CheckoutHistory) -> PlanReading:
        """Capture the main cut and acquire inputs without evaluating the plan."""
        reading = self.observed(repo, todos, history)
        for declared in evidence:
            reading.prepare(declared)
        return reading

    def observe_reading(self, reading: PlanReading, evidence: tuple[PhaseEvidence, ...]) -> PlanReading:
        for declared in evidence:
            declared.recorded(reading)
        self.retain(reading)
        return reading

    def read(self, repo: Repo, todos: tuple[Todo, ...],
             evidence: tuple[PhaseEvidence, ...]) -> PlanReading:
        return self.observe_reading(self.prepare_reading(
            repo, todos, evidence, history=Checkout(repo.checkout).read()), evidence)

    def retain(self, reading: PlanReading) -> None:
        """An ordinary command releases its reading when its caller does."""
        pass


class RetainedPlanReadings(PlanReadings):
    """Keep completed private snapshots; consumers may extend their own readings."""

    def __init__(self) -> None:
        super().__init__()
        self.plans: tuple[PlanReading, ...] = ()
        self.lock = Lock()

    def observed(
        self, repo: Repo, todos: tuple[Todo, ...], history: CheckoutHistory,
    ) -> PlanReading:
        current = super().observed(repo, todos, history)
        with self.lock:
            plans = self.plans
        for previous in plans:
            if previous.main.repo == repo:
                return previous.following(todos, current.main)
        return current

    def retain(self, reading: PlanReading) -> None:
        # Keep the pinned commit, parsed modules and stable answers, not the pass's graph.
        retained = reading.following(reading.todos, replace(
            reading.main, history=Checkout(reading.main.repo.checkout).read()))
        with self.lock:
            self.plans = (*(plan for plan in self.plans
                            if plan.main.repo != retained.main.repo), retained)


class PhaseEvidence(ABC):
    def prepare(self, reading: PlanReading) -> None:
        """Most evidence needs only the selected local tree."""

    def recorded(self, reading: PlanReading) -> None:
        reading.read(self)

    def retained(self, previous: MainTree, current: MainTree) -> bool:
        """Main-derived answers remain true of the same immutable tree."""
        return previous == current

    @abstractmethod
    def holds(self, reading: PlanReading) -> bool: ...

    @abstractmethod
    def describe(self) -> str: ...

    def sites(self, reading: PlanReading) -> tuple[str, ...]:
        """Each ``PATH:LINE`` that keeps this evidence from holding; evidence naming no line
        has none, and its description says what is missing."""
        return ()


class MainEvidence(PhaseEvidence):
    """Read the selected immutable main; PlanReading owns the answer's lifetime."""

    def holds(self, reading: PlanReading) -> bool:
        return self.read(reading.main)

    @abstractmethod
    def read(self, main: MainTree) -> bool: ...

    def sites(self, reading: PlanReading) -> tuple[str, ...]:
        return self.located(reading.main)

    def located(self, main: MainTree) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class CommitMerged(MainEvidence):
    sha: str

    def prepare(self, reading: PlanReading) -> None:
        Repo.require_commits(reading.main.repo.checkout, reading.main.repo.remote,
                             self.sha, reading.main.commit)
        reading.main.history.include(self.sha, reading.main.commit)

    def read(self, main: MainTree) -> bool:
        return main.contains(self.sha)

    def describe(self) -> str:
        return f"{self.sha} on main"


@dataclass(frozen=True)
class PathsAbsent(MainEvidence):
    paths: tuple[str, ...]

    def read(self, main: MainTree) -> bool:
        return not any(main.has(path) for path in self.paths)

    def describe(self) -> str:
        return "absent from main: " + ", ".join(self.paths)


@dataclass(frozen=True)
class PatternEvidence(MainEvidence):
    """A ``git grep -E`` count on main, judged by the member."""

    pattern: str
    paths: tuple[str, ...] = ("src",)

    def read(self, main: MainTree) -> bool:
        return self.satisfied(main.count(self.pattern, self.searched(main)))

    def searched(self, main: MainTree) -> tuple[str, ...]:
        """``paths`` without the phase declarations, which spell every pattern they ban."""
        from .plan_phases import PlanPhase

        declaration = main.repo.module_file(PlanPhase.__module__)
        return (*self.paths, f":!{declaration.as_posix()}")

    @abstractmethod
    def satisfied(self, count: int) -> bool: ...


@dataclass(frozen=True)
class PatternAbsent(PatternEvidence):
    def located(self, main: MainTree) -> tuple[str, ...]:
        return tuple(f"{path}:{line}" for path, line in
                     sorted(main.matching_lines(self.pattern, self.searched(main))))

    def satisfied(self, count: int) -> bool:
        return count == 0

    def describe(self) -> str:
        return f"no {self.pattern!r} in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class PatternPresent(PatternEvidence):
    def satisfied(self, count: int) -> bool:
        return count > 0

    def describe(self) -> str:
        return f"{self.pattern!r} in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class AuditPatternPresent(PatternPresent):
    """A pattern in the audit package of the installed NominalRefactorAdvisor: code this
    repository runs from that repository, which main's lock pins by commit."""

    def read(self, main: MainTree) -> bool:
        package = main.census.audit_package()
        found = re.compile(self.pattern, re.MULTILINE)
        return self.satisfied(sum(len(found.findall(module.read_text(encoding="utf-8")))
                                  for module in package.rglob("*.py")))

    def describe(self) -> str:
        return f"{self.pattern!r} in the installed NominalRefactorAdvisor audit package"


@dataclass(frozen=True)
class DocumentPatternPresent(PatternPresent):
    """A recorded document fact, rather than executable Python source."""

    def read(self, main: MainTree) -> bool:
        return bool(main.text_grep("-l", pattern=self.pattern, paths=self.searched(main)))


class BusStorageAbsent(MainEvidence, ast.NodeVisitor):
    """Read executable bus references, not text describing their deletion."""

    symbols = re.compile(r"WireLog|wire_log|_wire_lock_path|has_private_wire_fields")
    filenames = re.compile(r"bus[.]jsonl|bus_meta[.]json")
    lock_name: ClassVar[str] = "wire"
    path_methods: ClassVar[tuple[str, ...]] = (Path.with_name.__name__, Path.joinpath.__name__)
    found = False

    def read(self, main: MainTree) -> bool:
        scan = type(self)()
        for module in main.modules(("src",)):
            scan.visit(module.module)
        return not scan.found

    def describe(self) -> str:
        return "no known physical bus symbols, file literals or lock paths in Python source on main"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.found |= bool(self.symbols.fullmatch(node.name))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.found |= bool(self.symbols.fullmatch(node.name))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.found |= bool(self.symbols.fullmatch((node.module or "").rpartition(".")[2]))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.found |= bool(self.symbols.fullmatch(node.id))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.found |= bool(self.symbols.fullmatch(node.attr))
        self.generic_visit(node)

    def visit_alias(self, node: ast.alias) -> None:
        self.found |= bool(self.symbols.fullmatch(node.name.rpartition(".")[2]))

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.found |= bool(self.filenames.fullmatch(node.value))

    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return  # A docstring or inert prose cannot open a physical bus.
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        match node:
            case ast.BinOp(op=ast.Div(), right=ast.Constant(value=value)):
                self.found |= value == self.lock_name
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        match node:
            case ast.Call(func=ast.Attribute(attr=method), args=[*_, ast.Constant(value=value)]):
                self.found |= method in self.path_methods and value == self.lock_name
        self.generic_visit(node)


@dataclass(frozen=True)
class AllEvidence(PhaseEvidence):
    parts: tuple[PhaseEvidence, ...]

    def prepare(self, reading: PlanReading) -> None:
        for part in self.parts:
            reading.prepare(part)

    def recorded(self, reading: PlanReading) -> None:
        for part in self.parts:
            part.recorded(reading)
        super().recorded(reading)

    def retained(self, previous: MainTree, current: MainTree) -> bool:
        # Derive from this pass's parts, including a source that recovered from failure.
        return False

    def holds(self, reading: PlanReading) -> bool:
        return all(reading.holds(part) for part in self.parts)

    def sites(self, reading: PlanReading) -> tuple[str, ...]:
        return tuple(site for part in self.parts if not reading.holds(part)
                     for site in part.sites(reading))

    def describe(self) -> str:
        return "; ".join(part.describe() for part in self.parts)


@dataclass(frozen=True)
class ParityEvidence(AllEvidence):
    """Native owners supply the facts; the historical comparison matrix cannot overrule them."""


@dataclass(frozen=True)
class BranchRule:
    """An active GitHub rule, including its actual merge-queue requirement."""

    ruleset: int
    required: tuple[str, ...]
    queue: bool

    def requires(self, check: str) -> bool:
        return check in self.required


@dataclass(frozen=True)
class BypassActor:
    """A GitHub ruleset actor, decoded once by its declared identity fields."""

    actor_type: str
    actor_id: int


@dataclass(frozen=True)
class RulesetGates(PhaseEvidence):
    """Main requires source review, explicit merge permission and the queue, without bypass.

    GitHub policy changes without a main commit; read it once per reading.
    """

    def retained(self, previous: MainTree, current: MainTree) -> bool:
        """GitHub policy can change without a main move; read it once each pass."""
        return False

    rules_query: ClassVar[str] = (
        "[.[] | {ruleset: .ruleset_id, required: [.parameters.required_status_checks[]?.context],"
        " queue: (.type == \"merge_queue\")}]"
    )
    bypass_query: ClassVar[str] = (
        ".bypass_actors | map({"
        + ", ".join(entry.key for entry in FieldCodec.argument_fields(BypassActor))
        + "})"
    )

    def holds(self, reading: PlanReading) -> bool:
        main = reading.main
        rules = FieldCodec.decode(
            tuple[BranchRule, ...],
            self.api(main, f"rules/branches/{main.branch}", self.rules_query),
        )
        enforced = {ruleset for ruleset in {rule.ruleset for rule in rules}
                    if self.no_bypass(main, ruleset)}
        protected = tuple(rule for rule in rules if rule.ruleset in enforced)
        return (all(any(rule.requires(context) for rule in protected)
                    for context in MergeHeadReview.required_contexts())
                and any(rule.queue for rule in protected))

    def no_bypass(self, main: MainTree, ruleset: int) -> bool:
        actors = FieldCodec.decode(
            tuple[BypassActor, ...],
            self.api(main, f"rulesets/{ruleset}", self.bypass_query),
        )
        return not actors

    @staticmethod
    def api(main: MainTree, path: str, query: str) -> object:
        read = main.repo.github.rest(path, "--jq", query)
        if read.returncode:
            raise UnreadableEvidenceError(read.stderr.strip() or f"GitHub {path} could not be read")
        return FieldCodec.parse(read.stdout)

    def describe(self) -> str:
        return (
            f"active rules on {MainTree.branch} require "
            + " and ".join(MergeHeadReview.required_contexts())
            + " directly and an actual merge queue, with no bypass actors"
        )


class ErasedBody(ast.NodeTransformer):
    """A function's shape with identifiers, literals and attribute names erased, as the
    duplicate scan compared them."""

    def visit_Name(self, node: ast.Name) -> ast.AST:
        return ast.copy_location(ast.Name("_", node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        return ast.copy_location(ast.arg("_"), node)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        return ast.copy_location(ast.Attribute(node.value, "_", node.ctx), node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        return ast.copy_location(ast.Constant(None), node)

    @classmethod
    def of(cls, function: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        erased = cls().visit(copy.deepcopy(function))
        return ast.dump(erased.args) + ast.dump(ast.Module(erased.body, []))


class NamedFunctions(ast.NodeVisitor):
    """The functions and coroutines, nested ones included, named one of ``names``."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        self.found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.collect(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.collect(node)

    def collect(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name in self.names:
            self.found.append(node)
        self.generic_visit(node)

    @classmethod
    def of(
        cls, names: tuple[str, ...], tree: ast.AST
    ) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        functions = cls(names)
        functions.visit(tree)
        return functions.found


class Classes:
    """Every class statement in the given trees, nested ones included."""

    @staticmethod
    def of(trees: Iterable[ast.AST]) -> list[ast.ClassDef]:
        return [node for tree in trees for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


@dataclass(frozen=True)
class NoExactCopies(MainEvidence):
    """No two functions named one of ``names`` across ``paths`` share one erased body."""

    names: tuple[str, ...]
    paths: tuple[str, ...]

    def read(self, main: MainTree) -> bool:
        shapes = [
            ErasedBody.of(function)
            for module in main.present_modules(self.paths)
            for function in NamedFunctions.of(self.names, module.module)
        ]
        return len(shapes) == len(set(shapes))

    def describe(self) -> str:
        return f"no exact copies of {'/'.join(self.names)} in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class NoGodFunction(MainEvidence):
    """The census finds no god function named ``name`` in ``path``."""

    path: str
    name: str

    def read(self, main: MainTree) -> bool:
        return not any(
            main.census.god_function(module, function)
            for module in main.present_modules((self.path,))
            for function in NamedFunctions.of((self.name,), module.module)
        )

    def describe(self) -> str:
        return f"no god function {self.name} in {self.path} on main"


@dataclass(frozen=True)
class NoAbsenceProbes(MainEvidence):
    """The census's foreign absence probe (IDEN-3) counts zero in ``paths``; a plain optional
    value with no behaviour stays ``None``."""

    paths: tuple[str, ...]

    def read(self, main: MainTree) -> bool:
        """Every node is offered to the probe, whose own pattern decides what it counts."""
        return not any(
            main.census.absence_probes(node)
            for module in main.present_modules(self.paths)
            for node in ast.walk(module.module)
        )

    def describe(self) -> str:
        return f"no foreign absence probes in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class ClassFlagsAbsent(MainEvidence):
    """No line matching ``pattern`` in ``paths`` is a statement of a class body. A function's
    local or a dataclass field default of the same name is not a declared flag."""

    pattern: str
    paths: tuple[str, ...] = ("src",)
    owners: tuple[QualifiedName, ...] = ()

    def read(self, main: MainTree) -> bool:
        matched = main.matching_lines(self.pattern, self.paths)
        if self.owners:
            scopes = DeclaredScopes.of(main, self.paths)
            statements = (
                (declared.path, statement.lineno)
                for owner in self.owners
                for declared in scopes.declared(owner)
                for statement in declared.node.body
            )
        else:
            statements = (
                (path, statement.lineno)
                for path in {path for path, _ in matched}
                for declared in Classes.of((main.module(path).module,))
                for statement in declared.body
            )
        return not any(statement in matched for statement in statements)

    def describe(self) -> str:
        owners = f" of {', '.join(str(owner) for owner in self.owners)}" if self.owners else ""
        return f"no class body line {self.pattern!r}{owners} in {' '.join(self.paths)} on main"


class NamedClasses(ast.NodeVisitor):
    """The classes a base list or an ``isinstance`` target names, each by its last dotted part.
    A subscripted or computed base names none."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.names.add(node.attr)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """``Generic[T]`` names no class of ``src``."""

    def visit_Call(self, node: ast.Call) -> None:
        """A base computed by a call names no class."""

    @classmethod
    def of(cls, nodes: Iterable[ast.expr]) -> set[str]:
        named = cls()
        for node in nodes:
            named.visit(node)
        return named.names


class ScopeBindings(ast.NodeVisitor):
    """Names bound by one lexical scope, without borrowing a nested scope's locals."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_arg(self, node: ast.arg) -> None:
        self.names.add(node.arg)

    def visit_alias(self, node: ast.alias) -> None:
        self.names.add(node.asname or node.name.split(".")[0])

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)
        for value in (*node.decorator_list, *node.args.defaults,
                      *(default for default in node.args.kw_defaults if default is not None)):
            self.visit(value)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)
        for value in (*node.decorator_list, *node.bases, *node.keywords):
            self.visit(value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """A lambda's locals do not bind names in its enclosing scope."""

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.names.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.names.add(node.rest)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.names.update(node.names)

    @classmethod
    def of(cls, nodes: Iterable[ast.AST]) -> set[str]:
        bindings = cls()
        for node in nodes:
            bindings.visit(node)
        return bindings.names


class ClassChecks(ast.NodeVisitor):
    """The classes the visited modules declare and the classes their ``isinstance`` calls
    name."""

    def __init__(self) -> None:
        self.declared: set[str] = set()
        self.path = ""
        self.checked: list[tuple[str, int, frozenset[str]]] = []
        self._scopes: list[set[str]] = []
        self._methods: set[ast.FunctionDef | ast.AsyncFunctionDef] = set()
        self._comparison_guards: set[ast.Call] = set()

    def visit_Module(self, node: ast.Module) -> None:
        self._scopes.append(ScopeBindings.of(node.body))
        self.generic_visit(node)
        self._scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.declared.add(node.name)
        self._methods.update(member for member in node.body
                             if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scopes.append(ScopeBindings.of((node.args, *node.body)))
        arguments = (*node.args.posonlyargs, *node.args.args)
        if (node in self._methods and not node.decorator_list
            and node.name in ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")
            and len(arguments) == 2 and node.args.vararg is None and node.args.kwarg is None
            and not node.args.kwonlyargs
            and not any({"isinstance", "NotImplemented", "*"} & scope for scope in self._scopes)):
            for statement in node.body:
                match statement:
                    case ast.If(
                        test=ast.UnaryOp(op=ast.Not(), operand=ast.Call(
                            func=ast.Name(id="isinstance"),
                            args=[ast.Name(id=foreign), _], keywords=[],
                        ) as check),
                        body=[ast.Return(value=ast.Name(id="NotImplemented"))], orelse=[],
                    ) if foreign == arguments[1].arg:
                        self._comparison_guards.add(check)
        self.generic_visit(node)
        self._scopes.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        # Async special methods do not return Python's comparison result.
        self._scopes.append(ScopeBindings.of((node.args, *node.body)))
        self.generic_visit(node)
        self._scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        match node:
            case ast.Call(func=ast.Name(id=isinstance.__name__), args=[_, target]) \
                    if node not in self._comparison_guards:
                self.checked.append((self.path, node.lineno, frozenset(NamedClasses.of((target,)))))
        self.generic_visit(node)

    def owned(self) -> tuple[tuple[str, int], ...]:
        """The path and line of each check naming a class the visited modules declare."""
        return tuple((path, line) for path, line, named in self.checked if named & self.declared)


@dataclass(frozen=True)
class OwnClassChecksAbsent(MainEvidence):
    """No ``isinstance`` in ``src`` names a class that ``src`` declares; checks against AST,
    typing and SDK types stay."""

    def read(self, main: MainTree) -> bool:
        return not self.located(main)

    def located(self, main: MainTree) -> tuple[str, ...]:
        return tuple(f"{path}:{line}" for path, line in self.checks(main).owned())

    @staticmethod
    def checks(main: MainTree) -> ClassChecks:
        checks = ClassChecks()
        for module in main.modules(("src",)):
            checks.path = module.file_path
            checks.visit(module.module)
        return checks

    def describe(self) -> str:
        return "no isinstance against a class declared in src on main"


@dataclass(frozen=True)
class ParentsShared(MainEvidence):
    """Every named class, and every class declared in ``paths``, is a direct base of at least
    two classes in ``src``: a capability or mixin left with one member is folded back."""

    names: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    def read(self, main: MainTree) -> bool:
        declared = {
            *self.names,
            *(
                declared.name
                for declared in Classes.of(
                    module.module for module in main.present_modules(self.paths)
                )
            ),
        }
        members = Counter(
            base
            for member in Classes.of(module.module for module in main.modules(("src",)))
            for base in NamedClasses.of(member.bases) & declared
        )
        return all(members[name] >= 2 for name in declared)

    def describe(self) -> str:
        named = [*self.names, *self.paths]
        return "two members each for " + " ".join(named) + " on main"


@dataclass(frozen=True)
class QualifiedName:
    """A module or class as Python imports it, one part per dotted segment."""

    parts: tuple[str, ...]

    @classmethod
    def of(cls, dotted: str) -> QualifiedName:
        """An import statement's module text, split once where the AST hands it over."""
        return cls(tuple(part for part in dotted.split(".") if part))

    @classmethod
    def of_class(cls, declared: type) -> QualifiedName:
        """A live class's name, as its module and qualified name declare it."""
        return cls.of(declared.__module__).joined(cls.of(declared.__qualname__))

    def child(self, name: str) -> QualifiedName:
        return QualifiedName((*self.parts, name))

    def joined(self, other: QualifiedName) -> QualifiedName:
        return QualifiedName((*self.parts, *other.parts))

    @property
    def parent(self) -> QualifiedName:
        return QualifiedName(self.parts[:-1])

    @property
    def name(self) -> str:
        return self.parts[-1]

    def ancestors(self) -> tuple[QualifiedName, ...]:
        """This name and each enclosing one, nearest first, down to the empty root."""
        return tuple(QualifiedName(self.parts[:end]) for end in range(len(self.parts), -1, -1))

    def __str__(self) -> str:
        return ".".join(self.parts)


@dataclass(frozen=True)
class Declaration:
    """One declaring statement on main and the module path that holds it, so two declarations
    spelled alike stay two."""

    path: str

    def under(self, paths: tuple[str, ...]) -> bool:
        return any(PurePosixPath(self.path).is_relative_to(path) for path in paths)


@dataclass(frozen=True)
class DeclaredClass(Declaration):
    node: ast.ClassDef


@dataclass(frozen=True)
class DeclaredFunction(Declaration):
    node: ast.FunctionDef | ast.AsyncFunctionDef


class ClassDeclarations(ast.NodeVisitor):
    """Each class and function a body declares, nested classes and methods under their outer
    class's name. A function body runs later, so nothing in it is declared at import."""

    def __init__(self, scopes: DeclaredScopes, path: str, prefix: QualifiedName) -> None:
        self.scopes = scopes
        self.path = path
        self.prefix = prefix

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = self.prefix.child(node.name)
        self.scopes.classes[qualified] = DeclaredClass(self.path, node)
        ClassDeclarations(self.scopes, self.path, qualified).generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """A function's body is not run at import."""
        self.scopes.functions[self.prefix.child(node.name)] = DeclaredFunction(self.path, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """A coroutine's body is not run at import."""
        self.scopes.functions[self.prefix.child(node.name)] = DeclaredFunction(self.path, node)


class ModuleBindings(ClassDeclarations):
    """What one module's top level binds, under ``if`` and ``try`` too: its classes, its
    imports and the values it assigns names."""

    def __init__(self, scopes: DeclaredScopes, path: str, module: QualifiedName,
                 package: QualifiedName) -> None:
        super().__init__(scopes, path, module)
        self.anchors = (QualifiedName(()), *package.ancestors())
        self.bound: dict[str, QualifiedName] = {}
        self.assigned: dict[str, list[tuple[int, ast.expr]]] = {}
        self.assignments: list[ast.Assign | ast.AnnAssign] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound[node.name] = self.prefix.child(node.name)
        super().visit_ClassDef(node)

    def visit_Import(self, node: ast.Import) -> None:
        """``import a.b`` binds ``a``; ``import a.b as c`` binds ``c`` to ``a.b``."""
        for alias in node.names:
            imported = QualifiedName.of(alias.name)
            self.bound[alias.asname or imported.parts[0]] = (
                imported if alias.asname else QualifiedName(imported.parts[:1])
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """A relative import climbs from this module's package, one level per dot."""
        origin = self.anchors[node.level].joined(QualifiedName.of(node.module or ""))
        for alias in node.names:
            self.bound[alias.asname or alias.name] = origin.child(alias.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.assignments.append(node)
        for target in node.targets:
            self.assigned.setdefault(ast.unparse(target), []).append((node.lineno, node.value))

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.assignments.append(node)
        match node:
            case ast.AnnAssign(target=target, value=ast.expr() as value):
                self.assigned.setdefault(ast.unparse(target), []).append((node.lineno, value))

    def reach(self, local: str) -> Iterator[QualifiedName]:
        """What this module's top level binds ``local`` to, if it binds it."""
        if local in self.bound:
            yield self.bound[local]

    def literal(self, local: str, before: int) -> Iterator[ast.expr]:
        """The last value this module's top level assigns ``local`` before line ``before``."""
        yield from [value for line, value in self.assigned.get(local, []) if line < before][-1:]


class DeclaredScopes:
    """The modules under ``paths`` on main, each resolving a name only through what its own top
    level binds. A class is found by its qualified name, the way Python imports it, never by
    its bare spelling in some other module; a name its module never binds reaches nothing."""

    built: ClassVar[dict[tuple[MainTree, tuple[str, ...]], DeclaredScopes]] = {}

    @classmethod
    def of(cls, main: MainTree, paths: tuple[str, ...]) -> DeclaredScopes:
        """One walk per main commit and roots, shared by every reader; a newer main of the same
        checkout drops the older walks."""
        if (main, paths) not in cls.built:
            cls.built = {
                (tree, roots): scopes
                for (tree, roots), scopes in cls.built.items()
                if tree.repo.checkout != main.repo.checkout or tree == main
            }
            cls.built[main, paths] = cls(main, paths)
        return cls.built[main, paths]

    def __init__(self, main: MainTree, paths: tuple[str, ...]) -> None:
        self.classes: dict[QualifiedName, DeclaredClass] = {}
        self.functions: dict[QualifiedName, DeclaredFunction] = {}
        self.modules: dict[QualifiedName, ModuleBindings] = {}
        self.module_of: dict[str, QualifiedName] = {}
        for module in main.modules(paths):
            name, package = self.dotted(module.file_path, paths)
            bindings = self.modules[name] = ModuleBindings(self, module.file_path, name, package)
            bindings.visit(module.module)
            self.module_of[module.file_path] = name

    @staticmethod
    def dotted(path: str, roots: tuple[str, ...]) -> tuple[QualifiedName, QualifiedName]:
        """The module's import name below the root that holds it, and its package; a package's
        ``__init__`` is the package itself."""
        root = next(root for root in roots if PurePosixPath(path).is_relative_to(root))
        relative = PurePosixPath(path).relative_to(root)
        package = QualifiedName(relative.parent.parts)
        return package.joined(QualifiedName.of(relative.stem.removeprefix("__init__"))), package

    def qualify(self, module: QualifiedName, expression: ast.expr) -> Iterator[QualifiedName]:
        """The qualified name an expression reads in ``module``, when its top level binds it."""
        match expression:
            case ast.Name(id=local):
                yield from self.modules[module].reach(local)
            case ast.Attribute(value=value, attr=member):
                yield from (owner.child(member) for owner in self.qualify(module, value))

    def declared(
        self, qualified: QualifiedName, seen: frozenset[QualifiedName] = frozenset()
    ) -> Iterator[DeclaredClass]:
        """The class a qualified name reaches, following a module's re-export of an import."""
        if qualified in self.classes:
            yield self.classes[qualified]
        elif qualified.parent in self.modules:
            for target in self.modules[qualified.parent].reach(qualified.name):
                if target not in seen:
                    yield from self.declared(target, seen | {qualified})

    def resolve(self, module: QualifiedName, expression: ast.expr) -> Iterator[DeclaredClass]:
        for qualified in self.qualify(module, expression):
            yield from self.declared(qualified)

    def parents(self, declared: DeclaredClass) -> Iterator[DeclaredClass]:
        """Each direct base declared under the roots, read through its module's bindings."""
        for base in declared.node.bases:
            for parent in self.resolve(self.module_of[declared.path], base):
                if parent != declared:
                    yield parent

    def lineage(self, declared: DeclaredClass) -> frozenset[DeclaredClass]:
        """The class and every class its bases reach under the roots."""
        found = {declared}
        pending = [declared]
        while pending:
            for parent in self.parents(pending.pop()):
                if parent not in found:
                    found.add(parent)
                    pending.append(parent)
        return frozenset(found)


@dataclass(frozen=True)
class DeclarationEvidence(MainEvidence, DeclaredFamily, affix="Declared"):
    """Whether ``paths`` on main declare a member's kind of declaration called ``name``, read
    from the walk of their import roots; each member's declared name is the kind it reads. The
    name is a string because it is the declared target of a declaration the phase's work may
    not have written yet."""

    name: str
    paths: tuple[str, ...] = ("src",)

    def read(self, main: MainTree) -> bool:
        roots = tuple(dict.fromkeys(PurePosixPath(path).parts[0] for path in self.paths))
        return any(
            qualified.name == self.name and declared.under(self.paths)
            for qualified, declared in self.declarations(DeclaredScopes.of(main, roots)).items()
        )

    @abstractmethod
    def declarations(self, scopes: DeclaredScopes) -> Mapping[QualifiedName, Declaration]: ...

    def describe(self) -> str:
        return f"{self.declared_name} {self.name} declared in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class ClassDeclared(DeclarationEvidence):
    """A class ``name`` declared under ``paths``."""

    def declarations(self, scopes: DeclaredScopes) -> Mapping[QualifiedName, Declaration]:
        return scopes.classes


@dataclass(frozen=True)
class FunctionDeclared(DeclarationEvidence):
    """A function or method ``name`` declared under ``paths``."""

    def declarations(self, scopes: DeclaredScopes) -> Mapping[QualifiedName, Declaration]:
        return scopes.functions


class DecoratorNames(NamedClasses):
    """The classes a decorator list names; ``@dataclass(frozen=True)`` names the class it calls."""

    def visit_Call(self, node: ast.Call) -> None:
        self.visit(node.func)


class ClassAnswers(ast.NodeVisitor):
    """Each member a class body declares by name, with its declaration. A field counts only on
    a dataclass, and a name ``type`` reads per class never counts."""

    def __init__(self, declared: ast.ClassDef, per_class: frozenset[str]) -> None:
        self.fields = dataclass.__name__ in DecoratorNames.of(declared.decorator_list)
        self.per_class = per_class
        self.found: dict[str, ast.stmt] = {}

    @classmethod
    def of(cls, declared: ast.ClassDef, per_class: frozenset[str]) -> dict[str, ast.stmt]:
        answers = cls(declared, per_class)
        for member in declared.body:
            answers.visit(member)
        return answers.found

    def record(self, name: str, member: ast.stmt) -> None:
        if name not in self.per_class:
            self.found[name] = member

    def generic_visit(self, node: ast.AST) -> None:
        """Only the body's own statements declare members."""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.record(node.name, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.record(node.name, node)

    def visit_Assign(self, node: ast.Assign) -> None:
        match node.targets:
            case [ast.Name(id=name)]:
                self.record(name, node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """A field is inherited only between dataclasses, and only one with a default restates."""
        match node:
            case ast.AnnAssign(target=ast.Name(id=name), value=ast.expr()) if self.fields:
                self.record(name, node)


@dataclass(frozen=True)
class RestatedMembersAbsent(MainEvidence):
    """No class in ``paths`` restates a member its direct base in ``paths`` declares: a method
    or class attribute equal to the base's, or a field equal to the base's when both are
    dataclasses, so the field is already inherited."""

    paths: tuple[str, ...] = ("src",)
    per_class: ClassVar[frozenset[str]] = frozenset({"__slots__"})
    """Names ``type`` reads from each class's own namespace, never through its bases."""

    def read(self, main: MainTree) -> bool:
        return not any(self.restatements(main))

    def located(self, main: MainTree) -> tuple[str, ...]:
        return tuple(self.restatements(main))

    def restatements(self, main: MainTree) -> Iterator[str]:
        """Each restated member, as ``PATH:LINE Class.member``."""
        scopes = DeclaredScopes.of(main, self.paths)
        for declared in scopes.classes.values():
            for parent in scopes.parents(declared):
                inherited = ClassAnswers.of(parent.node, self.per_class)
                for name, member in ClassAnswers.of(declared.node, self.per_class).items():
                    if name in inherited and ast.dump(inherited[name]) == ast.dump(member):
                        restated = QualifiedName((declared.node.name, name))
                        yield f"{declared.path}:{member.lineno} {restated}"

    def describe(self) -> str:
        return f"no member restating its base's in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class ClassRostersAbsent(MainEvidence):
    """No module or class body in ``paths`` binds a literal collection naming two or more
    classes ``paths`` declares, builtins beside them or not, ``*NAME`` read through the
    module's own bindings: a family's members derive from its registry and their order from
    its declarations. A class attribute a ``families`` member declares as a tuple of its own
    classes, as ``PlanPhase.after``, is a dependency between members, not a roster."""

    paths: tuple[str, ...] = ("src",)
    families: tuple[type, ...] = ()

    def read(self, main: MainTree) -> bool:
        return not any(self.rosters(main))

    def located(self, main: MainTree) -> tuple[str, ...]:
        return tuple(self.rosters(main))

    def relations(self, scopes: DeclaredScopes) -> dict[DeclaredClass, frozenset[str]]:
        """Each family's declaration under the roots, found by its qualified name, with the class
        attributes it declares as a tuple of its own classes."""
        return {
            declared: frozenset(
                name
                for name, hint in FieldCodec._types(family).items()
                if get_origin(hint) is ClassVar
                and get_args(hint) == (tuple[type[family], ...],)  # type: ignore[valid-type]
            )
            for family in self.families
            for declared in scopes.declared(QualifiedName.of_class(family))
        }

    @staticmethod
    def related(
        scopes: DeclaredScopes,
        relations: dict[DeclaredClass, frozenset[str]],
        declared: DeclaredClass,
    ) -> frozenset[str]:
        """The relations of every family the class descends from; any other class's ``after``
        is a roster like any other."""
        lineage = scopes.lineage(declared)
        return frozenset[str]().union(
            *(names for family, names in relations.items() if family in lineage)
        )

    def rosters(self, main: MainTree) -> Iterator[str]:
        """Each roster, as ``PATH:LINE NAME``."""
        scopes = DeclaredScopes.of(main, self.paths)
        relations = self.relations(scopes)
        for bindings in scopes.modules.values():
            bodies = (
                (QualifiedName(()), frozenset[str](), tuple(bindings.assignments)),
                *(
                    (
                        QualifiedName((declared.node.name,)),
                        self.related(scopes, relations, declared),
                        tuple(declared.node.body),
                    )
                    for declared in scopes.classes.values()
                    if declared.path == bindings.path
                ),
            )
            found = sorted(
                (statement.lineno, str(owner.child(ast.unparse(target))))
                for owner, related, body in bodies
                for statement in body
                for target, value in self.bound(statement)
                if ast.unparse(target) not in related
                and self.members(bindings, value, statement.lineno) >= 2
            )
            yield from (f"{bindings.path}:{line} {target}" for line, target in found)

    @staticmethod
    def bound(statement: ast.stmt) -> Iterator[tuple[ast.expr, ast.expr]]:
        match statement:
            case ast.Assign(targets=[target], value=value) | ast.AnnAssign(
                target=target, value=ast.expr() as value
            ):
                yield target, value

    def members(self, bindings: ModuleBindings, value: ast.expr, before: int) -> int:
        """How many declared classes a literal collection names, ``*NAME`` expanded."""
        match value:
            case ast.Call(args=[ast.Tuple() | ast.List() | ast.Set() as listed]) \
                    | (ast.Tuple() | ast.List() | ast.Set() as listed):
                return sum(self.member(bindings, element, before) for element in listed.elts)
        return 0

    def member(self, bindings: ModuleBindings, element: ast.expr, before: int) -> int:
        match element:
            case ast.Starred(value=ast.Name(id=local)):
                return sum(
                    self.members(bindings, value, before)
                    for value in bindings.literal(local, before)
                )
        return sum(1 for _ in bindings.scopes.resolve(bindings.prefix, element))

    def describe(self) -> str:
        return f"no literal roster of declared classes in {' '.join(self.paths)} on main"
