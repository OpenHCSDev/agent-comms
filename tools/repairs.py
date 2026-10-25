"""Repairs for what the debt ratchet counts, built as Nominal Refactor Advisor plans.

A repair declares the located measure whose occurrences it removes. It reads the files
of the working tree, writes one NRA operation per occurrence it can remove and keeps
every other occurrence with the reason it stays. NRA simulates the plan, so the diff
shows before any file changes; applying it goes through NRA's source-revision guards,
then the repository's import sorter over the touched files and Black over those Black
already accepted.

    python tools/repairs.py --root src --base origin/main --head HEAD
    python tools/repairs.py --root src --all --apply
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import ClassVar

from debt_ratchet import FunctionLocalImport, LocatedSource, compare, production_paths
from module_layers import Package, TopLevelNames
from nominal_refactor_advisor.codemod import (
    CodemodPlanSequence,
    CodemodSourceSnapshot,
    EnsureImportOperation,
    PatchTargetOperation,
    SourceRewriteTarget,
    SourceTextReplacement,
)

from agent_comms.declared_family import DeclaredFamily

type FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
type ImportNode = ast.Import | ast.ImportFrom
type Operation = EnsureImportOperation | PatchTargetOperation


def bound_names(statement: ImportNode) -> list[str]:
    """The names one import statement binds in its scope."""
    return [alias.asname or alias.name.split(".")[0] for alias in statement.names]


class Kept(ABC):
    """Why a repair leaves an occurrence of its measure in place; ``why`` reports it."""

    why: ClassVar[str]


class OutsidePackage(Kept):
    why = "outside the agent_comms package"


class Repair(DeclaredFamily, ABC):
    """One measure's repair over some files of the working tree."""

    repairs: ClassVar[type[LocatedSource]]

    def __init__(self, repo: Path, paths: Iterable[str]) -> None:
        self.repo, self.paths = repo, sorted(paths)
        self.left: Counter[type[Kept]] = Counter()

    @abstractmethod
    def file_operations(self, path: str, source: str) -> list[Operation]:
        """The operations removing this file's occurrences; each one left is tallied in
        ``left`` under the reason it stays."""

    def sequence(self) -> CodemodPlanSequence:
        return CodemodPlanSequence.from_operations(
            tuple(
                operation
                for path in self.paths
                for operation in self.file_operations(path, (self.repo / path).read_text())
            )
        )

    def file_target(self, path: str) -> SourceRewriteTarget:
        """A file of this repair's tree, named absolutely: NRA resolves a relative path
        against the process directory, which need not be the tree."""
        return SourceRewriteTarget(file_path=str(self.repo / path))

    def function_target(self, path: str, qualname: str) -> SourceRewriteTarget:
        return replace(self.file_target(path), qualname=qualname)

    @staticmethod
    def sources(sequence: CodemodPlanSequence) -> dict[str, str]:
        return {path: Path(path).read_text() for path in sequence.explicit_source_paths()}

    def simulation(self) -> tuple[str, int]:
        """NRA's diff of the plan and how many files it changes."""
        sequence = self.sequence()
        if not sequence.explicit_source_paths():
            return "", 0
        sources = self.sources(sequence)
        simulated = sequence.simulate(CodemodSourceSnapshot.from_source_mapping(sources))
        return simulated.unified_diff(sources), len(sources)

    def apply(self) -> tuple[str, ...]:
        """Apply the plan; the touched files then take the repository's import order, and
        those Black accepted before stay formatted."""
        sequence = self.sequence()
        if not sequence.explicit_source_paths():
            return ()
        sources = self.sources(sequence)
        touched = tuple(str(Path(path).relative_to(self.repo)) for path in sources)
        formatted = [
            path for path in touched if self.tool("black", "--check", "-q", path).returncode == 0
        ]
        sequence.simulate(CodemodSourceSnapshot.from_source_mapping(sources)).apply()
        self.tool("ruff", "check", "--quiet", "--select", "I001", "--fix", *touched)
        if formatted:
            self.tool("black", "-q", *formatted)
        return touched

    def tool(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", *arguments], cwd=self.repo, capture_output=True, text=True
        )


class ImportHoist(Repair):
    """Moves a function-local import to its module top when no import that runs at module
    load leads back to the module, judged with every move already accepted: two local
    imports naming each other each pass alone and deadlock together."""

    repairs = FunctionLocalImport

    def __init__(self, repo: Path, paths: Iterable[str]) -> None:
        super().__init__(repo, paths)
        self.package = Package(repo)
        self.graph = self.package.load_graph()
        self.modules = {
            str(path.relative_to(repo)): module for module, path in self.package.files.items()
        }
        self.targets: dict[tuple[str, int], set[str]] = {}
        for edge in self.package.imports:
            self.targets.setdefault((edge.source, edge.line), set()).add(edge.target)

    def file_operations(self, path: str, source: str) -> list[Operation]:
        if path not in self.modules:
            self.left[OutsidePackage] += FunctionLocalImport.count(source.encode(), path)
            return []
        hoisted = HoistedFile(self, path, self.modules[path], source)
        for local in hoisted.local_imports(hoisted.tree, []):
            HoistVerdict.of(local).take(local)
        return hoisted.operations()


@dataclass
class HoistedFile:
    """One file under the hoist: what its top level binds, and the imports moved so far."""

    hoist: ImportHoist
    path: str
    module: str
    source: str
    hoisted: dict[str, str] = field(default_factory=dict)
    """Each moved name, by the one import text that binds it at the top."""
    moved: dict[str, tuple[FunctionNode, list[ImportNode]]] = field(default_factory=dict)
    """Each function losing imports, by qualified name, with the imports it loses."""

    @cached_property
    def tree(self) -> ast.Module:
        return ast.parse(self.source)

    @cached_property
    def lines(self) -> list[str]:
        return self.source.splitlines(keepends=True)

    @cached_property
    def bound(self) -> frozenset[str]:
        return TopLevelNames.of(self.tree)

    @cached_property
    def imported(self) -> dict[str, str]:
        """Each name a top-level import binds, by that import's text."""
        return {
            name: ast.unparse(statement)
            for statement in self.tree.body
            if isinstance(statement, (ast.Import, ast.ImportFrom))
            for name in bound_names(statement)
        }

    def local_imports(self, node: ast.AST, scope: list[str]) -> Iterator[LocalImport]:
        """Each import that is a direct body statement of a function, at any nesting of
        classes and functions."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = [*scope, child.name]
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for statement in child.body:
                        if isinstance(statement, (ast.Import, ast.ImportFrom)):
                            yield LocalImport(self, ".".join(inner), child, statement)
                yield from self.local_imports(child, inner)

    def operations(self) -> list[Operation]:
        operations: list[Operation] = [
            EnsureImportOperation(target=self.hoist.file_target(self.path), import_source=text)
            for text in dict.fromkeys(self.hoisted.values())
        ]
        operations += [
            PatchTargetOperation(
                target=self.hoist.function_target(self.path, qualname),
                replacements=tuple(
                    SourceTextReplacement(old_source=removed, new_source="")
                    for removed in dict.fromkeys(self.removals(function, statements))
                ),
            )
            for qualname, (function, statements) in self.moved.items()
        ]
        return operations

    def removals(self, function: FunctionNode, removed: list[ImportNode]) -> list[str]:
        """Each moved import's line; when the function's whole leading import block moves,
        the blank line that separated it from the code goes with the block's last line."""
        body = function.body
        match body[0]:
            case ast.Expr(value=ast.Constant(value=str())):
                start = 1
            case _:
                start = 0
        block: list[ImportNode] = []
        for statement in body[start:]:
            if not isinstance(statement, (ast.Import, ast.ImportFrom)):
                break
            block.append(statement)
        texts = [self.lines[statement.lineno - 1] for statement in removed]
        if (
            block
            and all(statement in removed for statement in block)
            and not self.lines[block[-1].lineno].strip()
        ):
            texts[removed.index(block[-1])] += self.lines[block[-1].lineno]
        return texts


@dataclass(frozen=True)
class LocalImport:
    """One import written as a direct body statement of a function in a hoisted file."""

    file: HoistedFile
    qualname: str
    function: FunctionNode
    statement: ImportNode

    @cached_property
    def text(self) -> str:
        return ast.unparse(self.statement)

    @cached_property
    def names(self) -> set[str]:
        return set(bound_names(self.statement))

    @cached_property
    def inside(self) -> set[str]:
        """The agent_comms modules the statement imports."""
        return self.file.hoist.targets.get((self.file.module, self.statement.lineno), set())

    @cached_property
    def root(self) -> str:
        """The top-level package an absolute import names; empty for a relative one."""
        match self.statement:
            case ast.ImportFrom(level=0, module=str(name)) | ast.Import(
                names=[ast.alias(name=name), *_]
            ):
                return name.split(".")[0]
        return ""


class HoistVerdict(DeclaredFamily, ABC):
    """What the hoist does with one local import: the first member, in declaration order,
    that holds for it decides."""

    @classmethod
    @abstractmethod
    def holds(cls, local: LocalImport) -> bool: ...

    @classmethod
    @abstractmethod
    def take(cls, local: LocalImport) -> None: ...

    @classmethod
    def of(cls, local: LocalImport) -> type[HoistVerdict]:
        return next(verdict for verdict in cls.members_with(cls) if verdict.holds(local))


class KeptImport(Kept, HoistVerdict, ABC):
    """The import stays in its function, and the hoist tallies why."""

    @classmethod
    def take(cls, local: LocalImport) -> None:
        local.file.hoist.left[cls] += 1


class ThirdParty(KeptImport):
    why = "third-party"

    @classmethod
    def holds(cls, local: LocalImport) -> bool:
        return not local.inside and local.root not in sys.stdlib_module_names


class NotOnePlainLine(KeptImport):
    why = "not one plain line"

    @classmethod
    def holds(cls, local: LocalImport) -> bool:
        statement = local.statement
        return (
            statement.lineno != statement.end_lineno
            or local.file.lines[statement.lineno - 1].strip() != local.text
        )


class BoundOtherwise(KeptImport):
    why = "the module binds the name another way"

    @classmethod
    def holds(cls, local: LocalImport) -> bool:
        return any(
            name in local.file.bound and local.file.imported.get(name) != local.text
            for name in local.names
        )


class BoundByAnotherMove(KeptImport):
    why = "another moved import binds the name"

    @classmethod
    def holds(cls, local: LocalImport) -> bool:
        return any(local.file.hoisted.get(name, local.text) != local.text for name in local.names)


class Rebound(KeptImport):
    why = "the function rebinds the name"

    @classmethod
    def holds(cls, local: LocalImport) -> bool:
        rebinding = Rebinding(local)
        rebinding.visit(local.function)
        return rebinding.found


class WholeBody(KeptImport):
    why = "the import is the whole body"

    @classmethod
    def holds(cls, local: LocalImport) -> bool:
        return len(local.function.body) == 1


class ImportCycle(KeptImport):
    why = "a module-level import cycle runs through it"

    @classmethod
    def holds(cls, local: LocalImport) -> bool:
        return local.file.module in local.file.hoist.graph.reached(local.inside)


class Moves(HoistVerdict):
    """Nothing keeps the import: it moves to the module top, and later imports are
    judged with the move accepted."""

    @classmethod
    def holds(cls, local: LocalImport) -> bool:
        return True

    @classmethod
    def take(cls, local: LocalImport) -> None:
        local.file.hoist.graph.add(local.file.module, local.inside)
        local.file.hoisted.update(dict.fromkeys(local.names, local.text))
        local.file.moved.setdefault(local.qualname, (local.function, []))[1].append(local.statement)


class Rebinding(ast.NodeVisitor):
    """Whether a function binds one of a local import's names again, by assignment or by
    another import."""

    def __init__(self, local: LocalImport) -> None:
        self.local, self.found = local, False

    def visit_Name(self, node: ast.Name) -> None:
        match node:
            case ast.Name(id=name, ctx=ast.Store()) if name in self.local.names:
                self.found = True

    def visit_Import(self, node: ImportNode) -> None:
        if node is not self.local.statement and set(bound_names(node)) & self.local.names:
            self.found = True

    # NodeVisitor dispatches on the node's class name; both import forms bind alike.
    visit_ImportFrom = visit_Import  # noqa: N815


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", required=True, help="Repository-relative production source directory"
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Repair every file under the root")
    scope.add_argument(
        "--base", help="Repair the files whose repaired measures grew since this revision"
    )
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--apply", action="store_true", help="Apply the plan instead of showing its diff"
    )
    args = parser.parse_args()
    repo = Path.cwd()
    root = args.root.rstrip("/") + "/"
    if args.all:
        files = {
            measure: sorted(
                path for path in production_paths(repo, "HEAD") if path.startswith(root)
            )
            for measure in (repair.repairs for repair in Repair.members_with(Repair))
        }
    else:
        found = compare(
            repo,
            args.base,
            args.head,
            args.root,
            1,
            tuple(repair.repairs for repair in Repair.members_with(Repair)),
        ).found
        files = {
            measure: sorted(
                {site.path for key, sites in found.items() if measure.owns(key) for site in sites}
            )
            for measure in (repair.repairs for repair in Repair.members_with(Repair))
        }
    for repair in Repair.members_with(Repair):
        fixer = repair(repo, files[repair.repairs])
        if args.apply:
            touched = fixer.apply()
            print(f"{repair.__name__}: applied to {len(touched)} files", file=sys.stderr)
        else:
            diff, changed = fixer.simulation()
            print(diff, end="")
            print(f"{repair.__name__}: {changed} files would change", file=sys.stderr)
        for reason, count in fixer.left.most_common():
            print(f"{repair.__name__}: {count} left, {reason.why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
