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
import subprocess
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from agent_comms.commander_parity import ParityRow
from agent_comms.field_codec import DefaultExternalName, ExternalName, FieldCodec
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
        return self.git("cat-file", "-e", f"{self.commit}:{path}").returncode == 0

    def grep(self, *options: str, pattern: str, paths: tuple[str, ...]) -> list[str]:
        """``git grep -E`` rows under ``paths``; git pathspecs such as ``:!file`` exclude."""
        run = self.git("grep", *options, "-E", "-e", pattern, self.commit, "--", *paths)
        if run.returncode not in (0, 1):
            run.check_returncode()
        return run.stdout.splitlines()

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
        run = self.git("show", f"{self.commit}:{path}")
        run.check_returncode()
        return ParsedModule(path, run.stdout, ast.parse(run.stdout, filename=path), path)

    def present_modules(self, paths: tuple[str, ...]) -> Iterator[ParsedModule]:
        """The parsed modules of the ``paths`` main holds. Plans finish by deleting files, so
        a declared path main no longer holds has no module, and nothing it held is left."""
        checked = subprocess.run(
            ("git", "-C", str(self.repo.checkout), "cat-file", "--batch-check"),
            input="".join(f"{self.commit}:{path}\n" for path in paths),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.splitlines()
        for path, line in zip(paths, checked, strict=True):
            if not line.endswith(" missing"):
                yield self.module(path)

    def modules(self, paths: tuple[str, ...]) -> Iterator[ast.Module]:
        """Every Python module under ``paths``, read in one ``git cat-file --batch``."""
        listed = self.git("ls-tree", "-r", "--name-only", self.commit, "--", *paths)
        listed.check_returncode()
        names = [name for name in listed.stdout.splitlines() if name.endswith(".py")]
        batch = subprocess.run(
            ("git", "-C", str(self.repo.checkout), "cat-file", "--batch"),
            input="".join(f"{self.commit}:{name}\n" for name in names).encode(),
            capture_output=True,
            timeout=60,
            check=True,
        ).stdout
        for name in names:
            header, _, batch = batch.partition(b"\n")
            size = int(header.rsplit(b" ", 1)[1])
            yield ast.parse(batch[:size], filename=name)
            batch = batch[size + 1 :]

    def contains(self, sha: str) -> bool:
        return self.repo.contains(sha, self.commit)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", "-C", str(self.repo.checkout), *args),
            capture_output=True,
            text=True,
            timeout=60,
        )


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
        return self.satisfied(main.count(self.pattern, self.paths))

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
    found = False

    def read(self, main: MainTree) -> bool:
        scan = type(self)()
        for tree in main.modules(("src",)):
            scan.visit(tree)
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
            case ast.BinOp(op=ast.Div(), right=ast.Constant(value="wire")):
                self.found = True
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        match node:
            case ast.Call(func=ast.Attribute(attr="with_name" | "joinpath"),
                          args=[*_, ast.Constant(value="wire")]):
                self.found = True
        self.generic_visit(node)


@dataclass(frozen=True)
class AllEvidence(PhaseEvidence):
    parts: tuple[PhaseEvidence, ...]

    def holds(self, reading: PlanReading) -> bool:
        return all(reading.holds(part) for part in self.parts)

    def describe(self) -> str:
        return "; ".join(part.describe() for part in self.parts)


@dataclass(frozen=True)
class ParityEvidence(PhaseEvidence):
    """The native/current comparison rows, which own the cutover decision."""

    def holds(self, reading: PlanReading) -> bool:
        return ParityRow.done(reading.todos)

    def describe(self) -> str:
        return "every parity row passes"


class BypassMode(ExternalName, affix="Bypass"):
    """How a ruleset's bypass actor may skip its rules: GitHub's ``bypass_mode``, decoded
    once. A mode this family does not name is taken as able to skip them."""

    @classmethod
    def keeps_gate(cls) -> bool:
        return False


class AlwaysBypass(BypassMode):
    """Skips the rules on every merge."""


class PullRequestBypass(BypassMode):
    """Skips the rules only through a pull request it bypasses for; the queue still gates."""

    @classmethod
    def keeps_gate(cls) -> bool:
        return True


class UnlistedBypass(DefaultExternalName, BypassMode):
    """A mode GitHub names that this family does not."""


@dataclass(frozen=True)
class BranchRule:
    """An active rule GitHub applies to main: its ruleset and the status checks it requires."""

    ruleset: int
    required: tuple[str, ...]

    def requires(self, check: str) -> bool:
        return check in self.required


@dataclass(frozen=True)
class RulesetGates(PhaseEvidence):
    """The merge queue refuses a head without the review's verdict: an active rule on main
    requires the queue job that reads ``HeadReview.context``, and no actor of that rule's
    ruleset skips it. A required check an actor skips gates nothing. GitHub's rules change
    without a main commit, so this is read once per reading, never cached by main's commit."""

    rules_query: ClassVar[str] = (
        "[.[] | {ruleset: .ruleset_id, required: [.parameters.required_status_checks[]?.context]}]"
    )
    bypass_query: ClassVar[str] = "[.bypass_actors[].bypass_mode]"

    def holds(self, reading: PlanReading) -> bool:
        main = reading.main
        rules = FieldCodec.decode(
            tuple[BranchRule, ...],
            self.api(main, f"rules/branches/{main.branch}", self.rules_query),
        )
        return any(
            self.unskipped(main, rule.ruleset)
            for rule in rules
            if rule.requires(HeadReview.queue_check)
        )

    def unskipped(self, main: MainTree, ruleset: int) -> bool:
        modes = FieldCodec.decode_external(
            tuple[type[BypassMode], ...],
            self.api(main, f"rulesets/{ruleset}", self.bypass_query),
            {},
        )
        return all(mode.keeps_gate() for mode in modes)

    @staticmethod
    def api(main: MainTree, path: str, query: str) -> object:
        read = main.repo.github.rest(path, "--jq", query)
        if read.returncode:
            raise UnreadableEvidenceError(read.stderr.strip() or f"GitHub {path} could not be read")
        return FieldCodec.parse(read.stdout)

    def describe(self) -> str:
        return (
            f"an active rule on {MainTree.branch} requires {HeadReview.queue_check}, the queue"
            f" job that reads {HeadReview.context}, and no actor of its ruleset skips it"
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

    def read(self, main: MainTree) -> bool:
        matched = main.matching_lines(self.pattern, self.paths)
        return not any(
            (path, statement.lineno) in matched
            for path in {path for path, _ in matched}
            for declared in Classes.of((main.module(path).tree,))
            for statement in declared.body
        )

    def describe(self) -> str:
        return f"no class body line {self.pattern!r} in {' '.join(self.paths)} on main"


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


class ClassChecks(ast.NodeVisitor):
    """The classes the visited modules declare and the classes their ``isinstance`` calls
    name."""

    def __init__(self) -> None:
        self.declared: set[str] = set()
        self.checked: set[str] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.declared.add(node.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        match node:
            case ast.Call(func=ast.Name(id=isinstance.__name__), args=[_, target]):
                self.checked |= NamedClasses.of((target,))
        self.generic_visit(node)


@dataclass(frozen=True)
class OwnClassChecksAbsent(MainEvidence):
    """No ``isinstance`` in ``src`` names a class that ``src`` declares; checks against AST,
    typing and SDK types stay."""

    def read(self, main: MainTree) -> bool:
        checks = ClassChecks()
        for module in main.modules(("src",)):
            checks.visit(module)
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
            for member in Classes.of(main.modules(("src",)))
            for base in NamedClasses.of(member.bases) & declared
        )
        return all(members[name] >= 2 for name in declared)

    def describe(self) -> str:
        named = [*self.names, *self.paths]
        return "two members each for " + " ".join(named) + " on main"
