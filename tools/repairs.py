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
from collections.abc import Iterable
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

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


class Repair(DeclaredFamily, ABC):
    """One measure's repair over some files of the working tree."""

    repairs: ClassVar[type[LocatedSource]]

    def __init__(self, repo: Path, paths: Iterable[str]) -> None:
        self.repo, self.paths = repo, sorted(paths)
        self.left: Counter[str] = Counter()

    @abstractmethod
    def file_operations(
        self, path: str, source: str
    ) -> list[EnsureImportOperation | PatchTargetOperation]:
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

    def target(self, path: str, qualname: str | None = None) -> SourceRewriteTarget:
        """A file of this repair's tree, named absolutely: NRA resolves a relative path
        against the process directory, which need not be the tree."""
        return SourceRewriteTarget(file_path=str(self.repo / path), qualname=qualname)

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

    def file_operations(
        self, path: str, source: str
    ) -> list[EnsureImportOperation | PatchTargetOperation]:
        if path not in self.modules:
            self.left["outside the agent_comms package"] += FunctionLocalImport.count(
                source.encode(), path
            )
            return []
        module, tree, lines = (
            self.modules[path],
            ast.parse(source),
            source.splitlines(keepends=True),
        )
        bound = TopLevelNames.of(tree)
        imported = {
            name: ast.unparse(statement)
            for statement in tree.body
            if isinstance(statement, (ast.Import, ast.ImportFrom))
            for name in self.bound(statement)
        }
        hoisted: dict[str, str] = {}
        moved: dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[ast.stmt]]] = {}
        for qualname, function, statement in self.local_imports(tree, []):
            text, names = ast.unparse(statement), set(self.bound(statement))
            inside = self.targets.get((module, statement.lineno), set())
            reason = self.stays(
                statement, function, text, names, inside, lines, bound, imported, hoisted, module
            )
            if reason:
                self.left[reason] += 1
                continue
            self.graph.add(module, inside)
            hoisted.update(dict.fromkeys(names, text))
            moved.setdefault(qualname, (function, []))[1].append(statement)
        operations: list[EnsureImportOperation | PatchTargetOperation] = [
            EnsureImportOperation(target=self.target(path), import_source=text)
            for text in dict.fromkeys(hoisted.values())
        ]
        operations += [
            PatchTargetOperation(
                target=self.target(path, qualname),
                replacements=tuple(
                    SourceTextReplacement(old_source=removed, new_source="")
                    for removed in dict.fromkeys(self.removals(lines, function, statements))
                ),
            )
            for qualname, (function, statements) in moved.items()
        ]
        return operations

    def stays(
        self,
        statement: ast.Import | ast.ImportFrom,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        text: str,
        names: set[str],
        inside: set[str],
        lines: list[str],
        bound: frozenset[str],
        imported: dict[str, str],
        hoisted: dict[str, str],
        module: str,
    ) -> str:
        """Why this import stays local, or the empty string when it moves."""
        root = self.imported_module(statement)
        if not inside and root not in sys.stdlib_module_names:
            return "third-party"
        if statement.lineno != statement.end_lineno or lines[statement.lineno - 1].strip() != text:
            return "not one plain line"
        if any(name in bound and imported.get(name) != text for name in names):
            return "the module binds the name another way"
        if any(hoisted.get(name, text) != text for name in names):
            return "another moved import binds the name"
        if self.rebinds(function, statement, names):
            return "the function rebinds the name"
        if len(function.body) == 1:
            return "the import is the whole body"
        if module in self.graph.reached(inside):
            return "a module-level import cycle runs through it"
        return ""

    @staticmethod
    def imported_module(statement: ast.Import | ast.ImportFrom) -> str:
        match statement:
            case ast.ImportFrom(level=0, module=str(name)):
                return name.split(".")[0]
            case ast.Import(names=[ast.alias(name=name), *_]):
                return name.split(".")[0]
        return ""

    @staticmethod
    def bound(statement: ast.Import | ast.ImportFrom) -> list[str]:
        return [alias.asname or alias.name.split(".")[0] for alias in statement.names]

    def local_imports(self, node: ast.AST, scope: list[str]):
        """Each import that is a direct body statement of a function, with the function's
        qualified name, at any nesting of classes and functions."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*FUNCTIONS, ast.ClassDef)):
                inner = [*scope, child.name]
                if isinstance(child, FUNCTIONS):
                    for statement in child.body:
                        if isinstance(statement, (ast.Import, ast.ImportFrom)):
                            yield ".".join(inner), child, statement
                yield from self.local_imports(child, inner)

    def rebinds(self, function: ast.AST, statement: ast.stmt, names: set[str]) -> bool:
        for node in ast.walk(function):
            match node:
                case ast.Name(id=name, ctx=ast.Store()) if name in names:
                    return True
                case ast.Import() | ast.ImportFrom() if (
                    node is not statement and set(self.bound(node)) & names
                ):
                    return True
        return False

    @staticmethod
    def removals(
        lines: list[str], function: ast.FunctionDef | ast.AsyncFunctionDef, removed: list[ast.stmt]
    ) -> list[str]:
        """Each moved import's line; when the function's whole leading import block moves,
        the blank line that separated it from the code goes with the block's last line."""
        body = function.body
        match body[0]:
            case ast.Expr(value=ast.Constant(value=str())):
                start = 1
            case _:
                start = 0
        block: list[ast.stmt] = []
        for statement in body[start:]:
            if not isinstance(statement, (ast.Import, ast.ImportFrom)):
                break
            block.append(statement)
        texts = [lines[statement.lineno - 1] for statement in removed]
        if (
            block
            and all(statement in removed for statement in block)
            and not lines[block[-1].lineno].strip()
        ):
            texts[removed.index(block[-1])] += lines[block[-1].lineno]
        return texts


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
            print(f"{repair.__name__}: {count} left, {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
