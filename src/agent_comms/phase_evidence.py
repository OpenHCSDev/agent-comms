"""What proves a plan phase done: facts on its repository's fetched ``origin/main``, and for
the merge gate the repository's own GitHub rulesets.

Task acceptance never proves a phase. Each phase declares the evidence its plan's
"Done means" names, and ``PlanPhase.done`` reads only that evidence.
"""

from __future__ import annotations

import ast
import copy
import json
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.commander_parity import ParityRow
from agent_comms.repo import Repo

if TYPE_CHECKING:
    from agent_comms.todos import Todo


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

    def lines(self, pattern: str, paths: tuple[str, ...]) -> tuple[str, ...]:
        run = self.git("grep", "-h", "-E", "-e", pattern, self.commit, "--", *paths)
        if run.returncode not in (0, 1):
            run.check_returncode()
        return tuple(run.stdout.splitlines())

    def module(self, path: str) -> ast.Module:
        run = self.git("show", f"{self.commit}:{path}")
        run.check_returncode()
        return ast.parse(run.stdout, filename=path)

    def contains(self, sha: str) -> bool:
        return self.repo.contains(sha, self.commit)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", "-C", str(self.repo.checkout), *args),
            capture_output=True,
            text=True,
            timeout=60,
        )


@dataclass(frozen=True)
class PlanReading:
    """One look at the plans: the order history and the plan repository's main.

    Each evidence is read once per reading, however many phases wait on it."""

    todos: tuple[Todo, ...]
    main: MainTree
    found: dict[PhaseEvidence, bool] = field(default_factory=dict, compare=False, repr=False)

    def holds(self, evidence: PhaseEvidence) -> bool:
        if evidence not in self.found:
            self.found[evidence] = evidence.holds(self)
        return self.found[evidence]


class PhaseEvidence(ABC):
    @abstractmethod
    def holds(self, reading: PlanReading) -> bool: ...

    @abstractmethod
    def describe(self) -> str: ...


@dataclass(frozen=True)
class CommitMerged(PhaseEvidence):
    sha: str

    def holds(self, reading: PlanReading) -> bool:
        return reading.main.contains(self.sha)

    def describe(self) -> str:
        return f"{self.sha} on main"


@dataclass(frozen=True)
class PathsAbsent(PhaseEvidence):
    paths: tuple[str, ...]

    def holds(self, reading: PlanReading) -> bool:
        return not any(reading.main.has(path) for path in self.paths)

    def describe(self) -> str:
        return "absent from main: " + ", ".join(self.paths)


@dataclass(frozen=True)
class PatternEvidence(PhaseEvidence):
    """A ``git grep -E`` count on main, judged by the member."""

    pattern: str
    paths: tuple[str, ...] = ("src",)

    def holds(self, reading: PlanReading) -> bool:
        return self.satisfied(reading.main.count(self.pattern, self.paths))

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
class RulesetGates(PhaseEvidence):
    """The merge queue refuses a head without ``check``: an active ruleset requires it and no
    actor bypasses that ruleset always. A required check an admin bypasses gates nothing."""

    check: str

    def holds(self, reading: PlanReading) -> bool:
        name = reading.main.repo.github.to_wire()
        return any(
            self.gates(self.api(f"repos/{name}/rulesets/{ruleset['id']}"))
            for ruleset in self.api(f"repos/{name}/rulesets")
            if ruleset["enforcement"] == "active"
        )

    def gates(self, ruleset: dict) -> bool:
        required = {
            check["context"]
            for rule in ruleset["rules"]
            if rule["type"] == "required_status_checks"
            for check in rule["parameters"]["required_status_checks"]
        }
        return self.check in required and not any(
            actor["bypass_mode"] == "always" for actor in ruleset["bypass_actors"]
        )

    @staticmethod
    def api(path: str):
        run = subprocess.run(("gh", "api", path), capture_output=True, text=True, timeout=60)
        run.check_returncode()
        return json.loads(run.stdout)

    def describe(self) -> str:
        return f"an active ruleset requires {self.check} with no always-bypass actor"


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
class NoExactCopies(PhaseEvidence):
    """No two functions named ``name`` across ``paths`` share one erased body."""

    name: str
    paths: tuple[str, ...]

    def holds(self, reading: PlanReading) -> bool:
        shapes = [
            ErasedBody.of(node)
            for path in self.paths
            for node in ast.walk(reading.main.module(path))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == self.name
        ]
        return len(shapes) == len(set(shapes))

    def describe(self) -> str:
        return f"no exact copies of {self.name} in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class FunctionLinesAtMost(PhaseEvidence):
    path: str
    name: str
    limit: int

    def holds(self, reading: PlanReading) -> bool:
        return all(
            node.end_lineno - node.lineno + 1 <= self.limit
            for node in ast.walk(reading.main.module(self.path))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == self.name
        )

    def describe(self) -> str:
        return f"{self.name} in {self.path} at most {self.limit} lines on main"


CLASS_HEADER = r"^\s*class [A-Za-z_][A-Za-z0-9_]*"


@dataclass(frozen=True)
class ClassFlagsAbsent(PhaseEvidence):
    """No class body in ``src`` declares one of ``names`` as a bool flag, bare or ``ClassVar``.
    A function's local or a dataclass field default of the same name is not a declared flag;
    the grep only picks the files the AST then reads."""

    names: tuple[str, ...]

    def holds(self, reading: PlanReading) -> bool:
        spelled = "|".join(self.names)
        candidates = reading.main.git(
            "grep", "-l", "-E", "-e", rf"^\s+({spelled})(: ClassVar\[bool\])? = (True|False)$",
            reading.main.commit, "--", "src",
        ).stdout.splitlines()
        return not any(
            self.flag(statement)
            for candidate in candidates
            for node in ast.walk(reading.main.module(candidate.split(":", 1)[1]))
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


@dataclass(frozen=True)
class OwnClassChecksAbsent(PhaseEvidence):
    """No ``isinstance`` in ``src`` names a class that ``src`` declares; checks against AST,
    typing and SDK types stay."""

    target = re.compile(r"isinstance\([^,()]+,\s*(\([^)]*\)|[A-Za-z_][\w.]*)")

    def holds(self, reading: PlanReading) -> bool:
        own = {
            re.match(CLASS_HEADER, line)[0].split("class ", 1)[1]
            for line in reading.main.lines(CLASS_HEADER, ("src",))
        }
        checked = {
            name.strip().rsplit(".", 1)[-1]
            for line in reading.main.lines(r"isinstance\(", ("src",))
            for match in self.target.finditer(line)
            for name in match[1].strip("()").split(",")
            if name.strip()
        }
        return not own & checked

    def describe(self) -> str:
        return "no isinstance against a class declared in src on main"


@dataclass(frozen=True)
class ParentsShared(PhaseEvidence):
    """Every class declared in ``paths`` has at least two direct subclasses in ``src``: a
    capability left with one member is folded back into it."""

    paths: tuple[str, ...]

    def holds(self, reading: PlanReading) -> bool:
        declared = {
            node.name
            for path in self.paths
            for node in reading.main.module(path).body
            if isinstance(node, ast.ClassDef)
        }
        bases = tuple(
            header.split("(", 1)[1] for header in reading.main.lines(CLASS_HEADER + r"\(", ("src",))
        )
        return all(
            sum(bool(re.search(rf"\b{name}\b", parents)) for parents in bases) >= 2
            for name in declared
        )

    def describe(self) -> str:
        return "every capability in " + " ".join(self.paths) + " has two members on main"
