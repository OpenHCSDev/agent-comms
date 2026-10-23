"""What proves a plan phase done: facts on its repository's fetched ``origin/main``, and for
the merge gate the repository's own GitHub rules.

Task acceptance never proves a phase. Each phase declares the evidence its plan's
"Done means" names, and ``PlanPhase.done`` reads only that evidence. Evidence that reads
only main is cached by main's commit, so a pass that finds main unmoved reads nothing again.
"""

from __future__ import annotations

import ast
import copy
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms.field_codec import FieldCodec
from agent_comms.head_review import HeadReview
from agent_comms.pinned_audit import ForeignAbsenceProbe, GodFunction, ParsedModule
from agent_comms.repo import Repo

if TYPE_CHECKING:
    from agent_comms.todos import Todo


@dataclass(frozen=True)
class MainTree:
    """One commit of a repository's fetched ``origin/main``; evidence never reads a worktree."""

    repo: Repo
    commit: str
    branch: ClassVar[str] = "main"

    @classmethod
    def of(cls, repo: Repo) -> MainTree:
        return cls(repo, Repo.git(repo.checkout, "rev-parse", f"origin/{cls.branch}").strip())

    def has(self, path: str) -> bool:
        return not self.missing(f"{self.commit}:{path}\n")[0]

    def missing(self, requests: str) -> tuple[bool, ...]:
        """For each ``REV:PATH`` line, whether this repository lacks it; any other failure
        is a GitCommandError, never an absence."""
        return tuple(line.endswith(" missing") for line in Repo.git(
            self.repo.checkout, "cat-file", "--batch-check", input=requests).splitlines())

    def grep(self, *options: str, pattern: str, paths: tuple[str, ...]) -> list[str]:
        """``git grep -E`` rows in this tree, like ``module`` and ``modules``.

        A submodule is another repository's tree, regardless of local recurse settings.
        Git pathspecs such as ``:!file`` exclude.
        """
        return list(Repo.grep(
            self.repo.checkout,
            "--no-recurse-submodules",
            *options,
            "-E",
            "-e",
            pattern,
            self.commit,
            "--",
            *paths,
        ))

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

    def module(self, path: str) -> ParsedModule:
        source = Repo.git(self.repo.checkout, "show", f"{self.commit}:{path}")
        return ParsedModule(path, source, ast.parse(source, filename=path), path)

    def present_modules(self, paths: tuple[str, ...]) -> Iterator[ParsedModule]:
        """The parsed modules of the ``paths`` main holds. Plans finish by deleting files, so
        a declared path main no longer holds has no module, and nothing it held is left."""
        missing = self.missing("".join(f"{self.commit}:{path}\n" for path in paths))
        for path, absent in zip(paths, missing, strict=True):
            if not absent:
                yield self.module(path)

    def modules(self, paths: tuple[str, ...]) -> Iterator[ParsedModule]:
        """Every Python module under ``paths``, read in one ``git cat-file --batch``."""
        listed = Repo.git(self.repo.checkout, "ls-tree", "-r", "--name-only", self.commit, "--",
                          *paths)
        names = [name for name in listed.splitlines() if name.endswith(".py")]
        batch = Repo.git_bytes(self.repo.checkout, "cat-file", "--batch",
                               input="".join(f"{self.commit}:{name}\n" for name in names))
        for name in names:
            header, _, batch = batch.partition(b"\n")
            size = int(header.rsplit(b" ", 1)[1])
            source = batch[:size].decode()
            yield ParsedModule(name, source, ast.parse(source, filename=name), name)
            batch = batch[size + 1 :]

    def contains(self, sha: str) -> bool:
        return self.repo.contains(sha, self.commit)


class UnreadableEvidenceError(Exception):
    """The evidence's source could not be read this pass; the phase is unknown, not failed."""


class EvidenceRead(ABC):
    """What one reading learned of one evidence."""

    @abstractmethod
    def held(self) -> bool: ...

    @abstractmethod
    def describe(self, evidence: PhaseEvidence) -> str: ...


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


@dataclass(frozen=True)
class PlanReading:
    """One look at the plans: the order history and the plan repository's main.

    Each evidence is read once per reading, however many phases wait on it."""

    todos: tuple[Todo, ...]
    main: MainTree
    reads: dict[PhaseEvidence, EvidenceRead] = field(
        default_factory=dict, compare=False, repr=False
    )

    def read(self, evidence: PhaseEvidence) -> EvidenceRead:
        if evidence not in self.reads:
            try:
                self.reads[evidence] = EvidenceFound(evidence.holds(self))
            except UnreadableEvidenceError as error:
                self.reads[evidence] = EvidenceUnread(str(error))
        return self.reads[evidence]

    def holds(self, evidence: PhaseEvidence) -> bool:
        return self.read(evidence).held()

    def describe(self, evidence: PhaseEvidence) -> str:
        return self.read(evidence).describe(evidence)


class PhaseEvidence(ABC):
    @abstractmethod
    def holds(self, reading: PlanReading) -> bool: ...

    @abstractmethod
    def describe(self) -> str: ...


class MainEvidence(PhaseEvidence):
    """Evidence that reads only main: one answer per main commit."""

    results: ClassVar[dict[tuple[MainTree, MainEvidence], bool]] = {}

    def holds(self, reading: PlanReading) -> bool:
        key = (reading.main, self)
        if key not in MainEvidence.results:
            MainEvidence.results = {
                (tree, evidence): held
                for (tree, evidence), held in MainEvidence.results.items()
                if tree.repo.checkout != reading.main.repo.checkout or tree == reading.main
            }
            MainEvidence.results[key] = self.read(reading.main)
        return MainEvidence.results[key]

    @abstractmethod
    def read(self, main: MainTree) -> bool: ...


@dataclass(frozen=True)
class CommitMerged(MainEvidence):
    sha: str

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
        from .plan_phases import PlanPhase

        declaration = main.repo.module_file(PlanPhase.__module__)
        return self.satisfied(main.count(self.pattern, (*self.paths, f":!{declaration.as_posix()}")))

    @abstractmethod
    def satisfied(self, count: int) -> bool: ...


@dataclass(frozen=True)
class PatternAbsent(PatternEvidence):
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
            scan.visit(module.tree)
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

    def holds(self, reading: PlanReading) -> bool:
        return all(reading.holds(part) for part in self.parts)

    def describe(self) -> str:
        return "; ".join(part.describe() for part in self.parts)


@dataclass(frozen=True)
class ParityEvidence(AllEvidence):
    """Native owners supply the facts; the historical comparison matrix cannot overrule them."""


@dataclass(frozen=True)
class BranchRule:
    """An active rule GitHub applies to main: its ruleset and the status checks it requires."""

    ruleset: int
    required: tuple[str, ...]

    def requires(self, check: str) -> bool:
        return check in self.required


@dataclass(frozen=True)
class RulesetGates(PhaseEvidence):
    """Main requires HeadReview's exact status and no actor can bypass that rule.

    GitHub's rules change without a main commit, so read them once per reading,
    never cache them by main's commit. Both bypass modes can skip a direct gate.
    """

    rules_query: ClassVar[str] = (
        "[.[] | {ruleset: .ruleset_id, required: [.parameters.required_status_checks[]?.context]}]"
    )
    bypass_query: ClassVar[str] = ".bypass_actors | isempty(.[])"

    def holds(self, reading: PlanReading) -> bool:
        main = reading.main
        rules = FieldCodec.decode(
            tuple[BranchRule, ...],
            self.api(main, f"rules/branches/{main.branch}", self.rules_query),
        )
        return any(
            self.unskipped(main, rule.ruleset)
            for rule in rules
            if rule.requires(HeadReview.context)
        )

    def unskipped(self, main: MainTree, ruleset: int) -> bool:
        return FieldCodec.decode(
            bool,
            self.api(main, f"rulesets/{ruleset}", self.bypass_query),
        )

    @staticmethod
    def api(main: MainTree, path: str, query: str) -> object:
        read = main.repo.github.rest(path, "--jq", query)
        if read.returncode:
            raise UnreadableEvidenceError(read.stderr.strip() or f"GitHub {path} could not be read")
        return FieldCodec.parse(read.stdout)

    def describe(self) -> str:
        return (
            f"an active rule on {MainTree.branch} requires {HeadReview.context} directly"
            " and no actor of its ruleset can bypass it"
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


class Classes(ast.NodeVisitor):
    """Every class statement in the visited trees, nested ones included."""

    def __init__(self) -> None:
        self.found: list[ast.ClassDef] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.found.append(node)
        self.generic_visit(node)

    @classmethod
    def of(cls, trees: Iterable[ast.AST]) -> list[ast.ClassDef]:
        classes = cls()
        for tree in trees:
            classes.visit(tree)
        return classes.found


@dataclass(frozen=True)
class NoExactCopies(MainEvidence):
    """No two functions named one of ``names`` across ``paths`` share one erased body."""

    names: tuple[str, ...]
    paths: tuple[str, ...]

    def read(self, main: MainTree) -> bool:
        shapes = [
            ErasedBody.of(function)
            for module in main.present_modules(self.paths)
            for function in NamedFunctions.of(self.names, module.tree)
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
            found
            for module in main.present_modules((self.path,))
            for function in NamedFunctions.of((self.name,), module.tree)
            for found in GodFunction.from_node(function, module)
        )

    def describe(self) -> str:
        return f"{self.name} in {self.path} at most {GodFunction.threshold} lines on main"


@dataclass(frozen=True)
class NoAbsenceProbes(MainEvidence):
    """The census's foreign absence probe (IDEN-3) counts zero in ``paths``; a plain optional
    value with no behaviour stays ``None``."""

    paths: tuple[str, ...]

    def read(self, main: MainTree) -> bool:
        """Every node is offered to the probe, whose own pattern decides what it counts."""
        return not any(
            ForeignAbsenceProbe.count(node)
            for module in main.present_modules(self.paths)
            for node in ast.walk(module.tree)
        )

    def describe(self) -> str:
        return f"no foreign absence probes in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class ClassFlagsAbsent(MainEvidence):
    """No line matching ``pattern`` in ``paths`` is a statement of a class body. A function's
    local or a dataclass field default of the same name is not a declared flag."""

    pattern: str
    paths: tuple[str, ...] = ("src",)
    owners: tuple[type, ...] = ()

    def read(self, main: MainTree) -> bool:
        matched = main.matching_lines(self.pattern, self.paths)
        class_names = frozenset(owner.__name__ for owner in self.owners)
        return not any(
            (path, statement.lineno) in matched
            for path in {path for path, _ in matched}
            for declared in Classes.of((main.module(path).tree,))
            if not class_names or declared.name in class_names
            for statement in declared.body
        )

    def describe(self) -> str:
        owners = f" of {', '.join(owner.__name__ for owner in self.owners)}" if self.owners else ""
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

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
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
        self.checked: set[str] = set()
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
                self.checked |= NamedClasses.of((target,))
        self.generic_visit(node)


@dataclass(frozen=True)
class OwnClassChecksAbsent(MainEvidence):
    """No ``isinstance`` in ``src`` names a class that ``src`` declares; checks against AST,
    typing and SDK types stay."""

    def read(self, main: MainTree) -> bool:
        checks = ClassChecks()
        for module in main.modules(("src",)):
            checks.visit(module.tree)
        return not checks.declared & checks.checked

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
                    module.tree for module in main.present_modules(self.paths)
                )
            ),
        }
        members = Counter(
            base
            for member in Classes.of(module.tree for module in main.modules(("src",)))
            for base in NamedClasses.of(member.bases) & declared
        )
        return all(members[name] >= 2 for name in declared)

    def describe(self) -> str:
        named = [*self.names, *self.paths]
        return "two members each for " + " ".join(named) + " on main"


@dataclass(frozen=True)
class RestatedMembersAbsent(MainEvidence):
    """No class in ``paths`` restates a member its direct base in ``paths`` declares: a method
    or class attribute equal to the base's, or a field equal to the base's when both are
    dataclasses, so the field is already inherited. Python declares ``__slots__`` per class."""

    paths: tuple[str, ...] = ("src",)

    def read(self, main: MainTree) -> bool:
        return not any(self.restatements(main))

    def restatements(self, main: MainTree) -> Iterator[str]:
        """Each restated member, as ``PATH:LINE Class.member``."""
        named: dict[str, list[tuple[str, ast.ClassDef]]] = {}
        for module in main.modules(self.paths):
            for declared in Classes.of((module.tree,)):
                named.setdefault(declared.name, []).append((module.path, declared))
        for path, declared in (pair for pairs in named.values() for pair in pairs):
            for parent in self.parents(named, path, declared):
                inherited = self.answers(parent)
                for name, (line, answer) in self.answers(declared).items():
                    if name in inherited and inherited[name][1] == answer:
                        yield f"{path}:{line} {declared.name}.{name}"

    @staticmethod
    def parents(
        named: dict[str, list[tuple[str, ast.ClassDef]]], path: str, declared: ast.ClassDef
    ) -> Iterator[ast.ClassDef]:
        """Each base that names exactly one class: a bare name prefers its own module's class,
        a dotted name another module's."""
        for base in declared.bases:
            match base:
                case ast.Name(id=name):
                    others = [pair for pair in named.get(name, []) if pair[1] is not declared]
                    found = [pair for pair in others if pair[0] == path] or others
                case ast.Attribute(attr=name):
                    found = [pair for pair in named.get(name, []) if pair[0] != path]
                case _:
                    found = []
            if len(found) == 1:
                yield found[0][1]

    @staticmethod
    def answers(declared: ast.ClassDef) -> dict[str, tuple[int, str]]:
        """Each member's line and its whole declaration; fields count only on a dataclass."""
        fields = dataclass.__name__ in NamedClasses.of(
            decorator.func if isinstance(decorator, ast.Call) else decorator
            for decorator in declared.decorator_list
        )
        found: dict[str, tuple[int, str]] = {}
        for member in declared.body:
            match member:
                case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                    found[name] = (member.lineno, ast.dump(member))
                case ast.Assign(targets=[ast.Name(id=name)]) if name != "__slots__":
                    found[name] = (member.lineno, ast.dump(member))
                case ast.AnnAssign(target=ast.Name(id=name), value=ast.expr()) \
                        if fields and name != "__slots__":
                    found[name] = (member.lineno, ast.dump(member))
        return found

    def describe(self) -> str:
        return f"no member restating its base's in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class ClassRostersAbsent(MainEvidence):
    """No module in ``paths`` binds a literal collection of two or more classes ``paths``
    declares: a family's members derive from its registry and their order from its
    declarations."""

    paths: tuple[str, ...] = ("src",)

    def read(self, main: MainTree) -> bool:
        return not any(self.rosters(main))

    def rosters(self, main: MainTree) -> Iterator[str]:
        """Each roster, as ``PATH:LINE NAME``."""
        modules = tuple(main.modules(self.paths))
        declared = {found.name for module in modules for found in Classes.of((module.tree,))}
        for module in modules:
            for statement in module.tree.body:
                match statement:
                    case ast.Assign(targets=[target], value=value) | ast.AnnAssign(
                        target=target, value=ast.expr() as value
                    ):
                        match value:
                            case ast.Call(args=[ast.Tuple() | ast.List() | ast.Set() as listed]) \
                                    | (ast.Tuple() | ast.List() | ast.Set() as listed):
                                if len(listed.elts) >= 2 and all(
                                    isinstance(element, ast.Name) and element.id in declared
                                    for element in listed.elts
                                ):
                                    yield f"{module.path}:{statement.lineno} {ast.unparse(target)}"

    def describe(self) -> str:
        return f"no literal roster of declared classes in {' '.join(self.paths)} on main"
