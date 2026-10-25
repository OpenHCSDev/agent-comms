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
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, ClassVar, get_args, get_origin, get_type_hints

from agent_comms.declared_family import DeclaredFamily
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
    source: ClassVar[str] = ".py"
    parsed: ClassVar[dict[str, tuple[str, ParsedModule]]] = {}

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
        """``git grep -E`` rows of this tree's Python modules, like ``module`` and ``modules``;
        the Markdown beside them quotes code it does not hold.

        A submodule is another repository's tree, regardless of local recurse settings.
        Git pathspecs such as ``:!file`` exclude.
        """
        return [row for row in Repo.grep(
            self.repo.checkout,
            "--no-recurse-submodules",
            *options,
            "-E",
            "-e",
            pattern,
            self.commit,
            "--",
            *paths,
        ) if row.split(":", 2)[1].endswith(self.source)]

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
        """Every Python module under ``paths``. Each path keeps its last parsed blob, so every
        evidence reading one commit parses it once, and the rest are read in one
        ``git cat-file --batch``."""
        listed = Repo.git(self.repo.checkout, "ls-tree", "-r", self.commit, "--", *paths)
        blobs = [(entry.split()[2], name) for entry, _, name in
                 (line.partition("\t") for line in listed.splitlines())
                 if name.endswith(self.source)]
        fresh = [(blob, name) for blob, name in blobs
                 if self.parsed.get(name, ("", None))[0] != blob]
        batch = Repo.git_bytes(self.repo.checkout, "cat-file", "--batch",
                               input="".join(f"{blob}\n" for blob, _ in fresh)) if fresh else b""
        for blob, name in fresh:
            header, _, batch = batch.partition(b"\n")
            size = int(header.rsplit(b" ", 1)[1])
            source = batch[:size].decode()
            self.parsed[name] = blob, ParsedModule(name, source,
                                                   ast.parse(source, filename=name), name)
            batch = batch[size + 1 :]
        yield from (self.parsed[name][1] for _, name in blobs)

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

    def sites(self, reading: PlanReading) -> tuple[str, ...]:
        """Each ``PATH:LINE`` that keeps this evidence from holding; evidence naming no line
        has none, and its description says what is missing."""
        return ()


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

    def sites(self, reading: PlanReading) -> tuple[str, ...]:
        return self.located(reading.main)

    def located(self, main: MainTree) -> tuple[str, ...]:
        return ()


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
                for declared in Classes.of((main.module(path).tree,))
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
            checks.path = module.path
            checks.visit(module.tree)
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
            name, package = self.dotted(module.path, paths)
            bindings = self.modules[name] = ModuleBindings(self, module.path, name, package)
            bindings.visit(module.tree)
            self.module_of[module.path] = name

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
                for name, hint in get_type_hints(family).items()
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
