"""What proves a plan phase done: facts on its repository's fetched ``origin/main``, and for
the merge gate the repository's own GitHub rulesets.

Task acceptance never proves a phase. Each phase declares the evidence its plan's
"Done means" names, and ``PlanPhase.done`` reads only that evidence. Evidence that reads
only main is cached by main's commit, so a pass that finds main unmoved reads nothing again.
"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from agent_comms.commander_parity import ParityRow
from agent_comms.head_review import HeadReview
from agent_comms.pinned_audit import ForeignAbsenceProbe, GodFunction
from agent_comms.repo import Repo

if TYPE_CHECKING:
    from agent_comms.todos import Todo


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class MainTree:
    """One commit of a repository's fetched ``origin/main``; evidence never reads a worktree."""

    repo: Repo
    commit: str

    @classmethod
    def of(cls, repo: Repo) -> MainTree:
        return cls(repo, Repo.git(repo.checkout, "rev-parse", "origin/main").strip())

    def has(self, path: str) -> bool:
        return self.git("cat-file", "-e", f"{self.commit}:{path}").returncode == 0

    def count(self, pattern: str, paths: tuple[str, ...]) -> int:
        """Matching lines under ``paths``; git pathspecs such as ``:!file`` exclude."""
        run = self.git("grep", "-c", "-E", "-e", pattern, self.commit, "--", *paths)
        if run.returncode not in (0, 1):
            run.check_returncode()
        return sum(int(line.rsplit(":", 1)[1]) for line in run.stdout.splitlines())

    def module(self, path: str) -> ast.Module:
        run = self.git("show", f"{self.commit}:{path}")
        run.check_returncode()
        return ast.parse(run.stdout, filename=path)

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
            batch = batch[size + 1:]

    def contains(self, sha: str) -> bool:
        return self.repo.contains(sha, self.commit)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", "-C", str(self.repo.checkout), *args),
            capture_output=True,
            text=True,
            timeout=60,
        )


class EvidenceUnknown(Exception):
    """The evidence's source could not be read this pass; the phase is unknown, not failed."""


@dataclass(frozen=True)
class PlanReading:
    """One look at the plans: the order history and the plan repository's main.

    Each evidence is read once per reading, however many phases wait on it. Evidence whose
    source cannot be read counts as not done and says why in its description."""

    todos: tuple[Todo, ...]
    main: MainTree
    found: dict[PhaseEvidence, bool] = field(default_factory=dict, compare=False, repr=False)
    unknown: dict[PhaseEvidence, str] = field(default_factory=dict, compare=False, repr=False)

    def holds(self, evidence: PhaseEvidence) -> bool:
        if evidence not in self.found:
            try:
                self.found[evidence] = evidence.holds(self)
            except EvidenceUnknown as error:
                self.unknown[evidence] = str(error)
                self.found[evidence] = False
        return self.found[evidence]

    def describe(self, evidence: PhaseEvidence) -> str:
        self.holds(evidence)
        reason = self.unknown.get(evidence)
        return evidence.describe() if reason is None else f"unknown ({reason}): {evidence.describe()}"


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
                cached: held for cached, held in MainEvidence.results.items()
                if cached[0].repo.checkout != reading.main.repo.checkout or cached[0] == reading.main
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


@dataclass(frozen=True)
class RulesetGates(MainEvidence):
    """The merge queue refuses a head without the review's verdict: an active ruleset requires
    the queue job that reads ``review.context`` and no actor bypasses that ruleset always. A
    required check an admin bypasses gates nothing. Read once per main commit."""

    review: type[HeadReview]

    def read(self, main: MainTree) -> bool:
        return any(
            self.gates(self.api(main, f"rulesets/{ruleset['id']}"))
            for ruleset in self.api(main, "rulesets")
            if ruleset["enforcement"] == "active"
        )

    def gates(self, ruleset: dict) -> bool:
        required = {
            check["context"]
            for rule in ruleset["rules"]
            if rule["type"] == "required_status_checks"
            for check in rule["parameters"]["required_status_checks"]
        }
        return self.review.queue_check in required and not any(
            actor["bypass_mode"] == "always" for actor in ruleset["bypass_actors"]
        )

    @staticmethod
    def api(main: MainTree, path: str):
        read = main.repo.github.rest(path)
        if read.returncode:
            raise EvidenceUnknown(read.stderr.strip() or f"GitHub {path} could not be read")
        return json.loads(read.stdout)

    def describe(self) -> str:
        return (f"an active ruleset requires {self.review.queue_check}, the queue job that reads"
                f" {self.review.context}, with no always-bypass actor")


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


@dataclass(frozen=True)
class NoExactCopies(MainEvidence):
    """No two functions named one of ``names`` across ``paths`` share one erased body."""

    names: tuple[str, ...]
    paths: tuple[str, ...]

    def read(self, main: MainTree) -> bool:
        shapes = [
            ErasedBody.of(node)
            for path in self.paths
            for node in ast.walk(main.module(path))
            if isinstance(node, FUNCTIONS) and node.name in self.names
        ]
        return len(shapes) == len(set(shapes))

    def describe(self) -> str:
        return f"no exact copies of {'/'.join(self.names)} in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class NoGodFunction(MainEvidence):
    """``name`` in ``path`` is under the census's god-function size."""

    path: str
    name: str

    def read(self, main: MainTree) -> bool:
        return all(
            node.end_lineno - node.lineno + 1 <= GodFunction.threshold
            for node in ast.walk(main.module(self.path))
            if isinstance(node, FUNCTIONS) and node.name == self.name
        )

    def describe(self) -> str:
        return f"{self.name} in {self.path} at most {GodFunction.threshold} lines on main"


@dataclass(frozen=True)
class NoAbsenceProbes(MainEvidence):
    """The census's foreign absence probe (IDEN-3) counts zero in ``paths``; a plain optional
    value with no behaviour stays ``None``."""

    paths: tuple[str, ...]

    def read(self, main: MainTree) -> bool:
        return not any(
            ForeignAbsenceProbe.count(node)
            for path in self.paths
            for node in ast.walk(main.module(path))
            if isinstance(node, ForeignAbsenceProbe.node_types)
        )

    def describe(self) -> str:
        return f"no foreign absence probes in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class ClassFlagsAbsent(MainEvidence):
    """No class body in ``src`` declares one of ``names`` as a bool flag, bare or ``ClassVar``.
    A function's local or a dataclass field default of the same name is not a declared flag;
    the grep only picks the files the AST then reads."""

    names: tuple[str, ...]

    def read(self, main: MainTree) -> bool:
        spelled = "|".join(self.names)
        candidates = main.git(
            "grep", "-l", "-E", "-e", rf"^\s+({spelled})(: ClassVar\[bool\])? = (True|False)$",
            main.commit, "--", "src",
        ).stdout.splitlines()
        return not any(
            self.flag(statement)
            for candidate in candidates
            for node in ast.walk(main.module(candidate.split(":", 1)[1]))
            if isinstance(node, ast.ClassDef)
            for statement in node.body
        )

    def flag(self, statement: ast.stmt) -> bool:
        match statement:
            case ast.Assign(targets=[ast.Name(id=name)], value=ast.Constant(value=bool())):
                return name in self.names
            case ast.AnnAssign(target=ast.Name(id=name), annotation=ast.Subscript(value=ast.Name("ClassVar")),
                               value=ast.Constant(value=bool())):
                return name in self.names
        return False

    def describe(self) -> str:
        return f"no class body flag {'|'.join(self.names)} on main"


def class_name(node: ast.expr) -> str | None:
    """The class a base or ``isinstance`` target names, by its last dotted part."""
    match node:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
    return None


@dataclass(frozen=True)
class OwnClassChecksAbsent(MainEvidence):
    """No ``isinstance`` in ``src`` names a class that ``src`` declares; checks against AST,
    typing and SDK types stay."""

    def read(self, main: MainTree) -> bool:
        own: set[str] = set()
        checked: set[str] = set()
        for module in main.modules(("src",)):
            for node in ast.walk(module):
                match node:
                    case ast.ClassDef(name=name):
                        own.add(name)
                    case ast.Call(func=ast.Name("isinstance"), args=[_, ast.Tuple(elts=targets)]):
                        checked.update(filter(None, map(class_name, targets)))
                    case ast.Call(func=ast.Name("isinstance"), args=[_, target]):
                        checked.update(filter(None, (class_name(target),)))
        return not own & checked

    def describe(self) -> str:
        return "no isinstance against a class declared in src on main"


@dataclass(frozen=True)
class ParentsShared(MainEvidence):
    """Every named class, and every class declared in ``paths``, is a direct base of at least
    two classes in ``src``: a capability or mixin left with one member is folded back."""

    names: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    def read(self, main: MainTree) -> bool:
        declared = {*self.names, *(
            node.name for path in self.paths for node in main.module(path).body
            if isinstance(node, ast.ClassDef)
        )}
        members = {name: 0 for name in declared}
        for module in main.modules(("src",)):
            for node in ast.walk(module):
                if isinstance(node, ast.ClassDef):
                    for base in {class_name(base) for base in node.bases} & declared:
                        members[base] += 1
        return all(count >= 2 for count in members.values())

    def describe(self) -> str:
        named = [*self.names, *self.paths]
        return "two members each for " + " ".join(named) + " on main"
