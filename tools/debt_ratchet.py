#!/usr/bin/env python3
"""Compare AST debt in every changed production Python path at two Git revisions."""

from __future__ import annotations

import argparse
import ast
import enum
import importlib
import io
import json
import multiprocessing
import os
import re
import subprocess
import sys
import tempfile
import tokenize
import tomllib
import typing
from abc import abstractmethod
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from functools import cache, cached_property, partial
from pathlib import Path

sys.path.insert(0, str(
    Path(__file__).resolve().parents[1] / subprocess.check_output(
        ["git", "config", "--file", str(Path(__file__).resolve().parents[1] / ".gitmodules"),
         "--get", "submodule.src/agent_comms/skills/refactor-audit.path"], text=True,
    ).strip() / "scripts"
))

import audit
from audit.findings import Package as AuditPackage, ParsedModule as AuditModule, RawShape
from audit.repository import Repository as AuditRepository
from audit.handler_declarations import BuiltinHandlerDeclarations
from audit.measures import FamilyFlattened as AuditFamilyFlattened
from audit.measures import ForeignAbsenceProbe as AuditForeignAbsenceProbe

from agent_comms.core_stamp import WheelStamp
from agent_comms.declared_family import DeclaredFamily
from cutover.carry import STEPS, Carry, Step, carry_parents
from stored_formats import StoredForm
from agent_comms.field_codec import FieldCodec, boundary_decoder, projected
from agent_comms.mro_dispatch import MroDispatch, handles

AUDIT_SCRIPTS = Path(audit.__path__[0]).parent


class Side(enum.Enum):
    """The two revisions a comparison measures; the value names a checker's cache."""

    BASE = "base"
    HEAD = "head"


class Measure(DeclaredFamily, affix="Measure"):
    @classmethod
    def blocks(cls, delta: dict[str, int]) -> bool:
        return any(value > 0 for key, value in delta.items()
                   if key.partition(":")[0] == cls.__name__)

    @staticmethod
    def environment(root: str, tree: Path) -> dict[str, str]:
        """The environment of a tool run in a revision extracted to the tree."""
        return dict(os.environ)

    @classmethod
    @abstractmethod
    def parts(cls, repo: Path, base: str, head: str, changed: set[str],
              root: str) -> tuple[Callable[[], object], ...]:
        """Independent pieces of the comparison; compare() runs each in a separate process."""

    @classmethod
    @abstractmethod
    def combine(cls, results: list, repo: Path, base: str, head: str, changed: set[str],
                root: str) -> tuple[dict[str, int], dict[str, int]]:
        """The before/after measurements from the results of parts(), in order."""

    @classmethod
    def align(cls, base: dict[str, int], head: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
        return base, head

    @classmethod
    def compare(cls, repo: Path, base: str, head: str, changed: set[str], root: str):
        """Return independent before/after measurements for this declaration."""
        parts = cls.parts(repo, base, head, changed, root)
        return cls.combine([part() for part in parts], repo, base, head, changed, root)


class ChangeMeasure(Measure):
    """A count of the change as a whole, taken in one piece."""

    @classmethod
    @abstractmethod
    def counted(cls, repo: Path, base: str, head: str, changed: set[str],
                root: str) -> tuple[dict[str, int], dict[str, int]]:
        """The before/after counts."""

    @classmethod
    def parts(cls, repo: Path, base: str, head: str, changed: set[str],
              root: str) -> tuple[Callable[[], object], ...]:
        return (partial(cls.counted, repo, base, head, changed, root),)

    @classmethod
    def combine(cls, results: list, repo: Path, base: str, head: str, changed: set[str],
                root: str) -> tuple[dict[str, int], dict[str, int]]:
        before, after = results[0]
        return before, after


class RevisionMeasure(Measure):
    """Each revision measured on its own, so the two run at once; align() pairs them."""

    @classmethod
    @abstractmethod
    def at(cls, repo: Path, ref: str, base: str, head: str, changed: set[str],
           root: str) -> dict[str, int]:
        """This revision's measurements."""

    @classmethod
    def parts(cls, repo: Path, base: str, head: str, changed: set[str],
              root: str) -> tuple[Callable[[], object], ...]:
        return tuple(partial(cls.at, repo, ref, base, head, changed, root) for ref in (base, head))

    @classmethod
    def combine(cls, results: list, repo: Path, base: str, head: str, changed: set[str],
                root: str) -> tuple[dict[str, int], dict[str, int]]:
        return cls.align(*results)


class InventoryMeasure[Reading](Measure):
    """A measure of every file a revision holds, not only the changed ones. A file the change
    did not touch reads the same at both revisions, so it is read once; the reading is split
    across processes."""

    @classmethod
    @abstractmethod
    def inventory(cls, repo: Path, ref: str, root: str) -> frozenset[str]:
        """The paths measured at this revision."""

    @classmethod
    @abstractmethod
    def reading(cls, path: str, source: bytes) -> Reading:
        """What the measure needs from one file, independent of the revision holding it."""

    @classmethod
    @abstractmethod
    def measured(cls, repo: Path, ref: str, base: str, head: str, readings: dict[str, Reading],
                 root: str) -> dict[str, int]:
        """This revision's measurements from its files' readings, in path order."""

    @classmethod
    def read(cls, repo: Path, ref: str, paths: list[str]) -> dict[str, Reading]:
        return {path: cls.reading(path, source)
                for path, source in GitMeasure.sources(repo, ref, set(paths))}

    @classmethod
    def parts(cls, repo: Path, base: str, head: str, changed: set[str],
              root: str) -> tuple[Callable[[], object], ...]:
        before, after = cls.inventory(repo, base, root), cls.inventory(repo, head, root)
        shared = sorted((before & after) - touched_paths(repo, base, head))
        shards = os.process_cpu_count() or 1
        return (partial(cls.read, repo, base, sorted(before.difference(shared))),
                partial(cls.read, repo, head, sorted(after.difference(shared))),
                *(partial(cls.read, repo, head, shared[shard::shards]) for shard in range(shards)))

    @classmethod
    def combine(cls, results: list, repo: Path, base: str, head: str, changed: set[str],
                root: str) -> tuple[dict[str, int], dict[str, int]]:
        before, after, *shards = results
        shared = {path: reading for shard in shards for path, reading in shard.items()}
        return cls.align(*(cls.measured(repo, ref, base, head,
                                        dict(sorted({**shared, **own}.items())), root)
                           for ref, own in ((base, before), (head, after))))


class GitMeasure(RevisionMeasure):
    """Shared Git collection and alignment for declaration-owned snapshots."""

    @classmethod
    @abstractmethod
    def snapshot(cls, sources: list[tuple[str, bytes]]) -> dict[str, int]:
        """Measure the selected source declarations."""

    @staticmethod
    def sources(repo: Path, ref: str, selected: set[str]) -> list[tuple[str, bytes]]:
        """Read the selected Git blobs with one batch command."""
        if not selected:
            return []
        tree = git(repo, "--literal-pathspecs", "ls-tree", "-rz", ref, "--", *sorted(selected))
        identities = {}
        for entry in tree.split(b"\0"):
            if entry:
                metadata, path = entry.split(b"\t", 1)
                _, kind, identity = metadata.split()
                if kind != b"blob":
                    raise ValueError(f"Expected a source blob at {path!r}")
                identities[path.decode()] = identity
        if identities.keys() != selected:
            raise ValueError("Git omitted a selected path")
        objects = dict.fromkeys(identities.values())
        stream = io.BytesIO(git(repo, "cat-file", "--batch", input=b"\n".join(objects) + b"\n"))
        blobs = {}
        for identity in objects:
            recorded, kind, size = stream.readline().split()
            if recorded != identity or kind != b"blob":
                raise ValueError("Git returned an unrelated object")
            length = int(size)
            content = stream.read(length)
            if len(content) != length or stream.read(1) != b"\n":
                raise ValueError("Git returned an incomplete object")
            blobs[identity] = content
        if stream.read(1):
            raise ValueError("Git returned unrequested objects")
        return [(path, blobs[identities[path]]) for path in sorted(selected)]

    @classmethod
    def at(cls, repo: Path, ref: str, base: str, head: str, changed: set[str],
           root: str) -> dict[str, int]:
        return cls.snapshot(cls.sources(repo, ref, changed & python_paths(repo, ref, root)))


class CountedSource(GitMeasure):
    """A measure that is one number per source file."""

    @classmethod
    @abstractmethod
    def count(cls, source: bytes, filename: str) -> int:
        """Return this file's count."""


class OccurrenceMeasure(CountedSource):
    """Shared additive counting for syntactic occurrence measures."""

    @staticmethod
    @abstractmethod
    def occurrences(node: ast.AST) -> int:
        """Return this node's contribution, excluding its descendants."""

    @staticmethod
    def sites(tree: ast.AST, filename: str) -> Iterator[ast.AST]:
        """The nodes this measure reads; every node unless a measure excludes some."""
        return ast.walk(tree)

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        tree = ast.parse(source, filename)
        return sum(cls.occurrences(node) for node in cls.sites(tree, filename))

    @classmethod
    def snapshot(cls, sources: list[tuple[str, bytes]]) -> dict[str, int]:
        return {cls.__name__: sum(cls.count(source, path) for path, source in sources)}


class PerFileMeasure(CountedSource):
    """An improvement in one file cannot hide a regression in another."""

    @classmethod
    def snapshot(cls, sources: list[tuple[str, bytes]]) -> dict[str, int]:
        return {f"{cls.__name__}:{path}": cls.count(source, path) for path, source in sources}

    @classmethod
    def align(cls, base: dict[str, int], head: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
        identities = sorted(base.keys() | head.keys())
        return ({key: base.get(key, 0) for key in identities},
                {key: head.get(key, 0) for key in identities})


class PerFileOccurrenceMeasure(PerFileMeasure, OccurrenceMeasure):
    """A per-node count, kept separately for each file."""


class CheckedRevision(Measure):
    """A checker's findings per changed file. Both revisions are checked under the head's
    configuration, so the difference is the code's alone. A path missing at one revision counts
    zero there, as in every per-file measure."""

    finding: typing.ClassVar[re.Pattern[str]]  # One output line per finding; group 1 is its path.

    @staticmethod
    @abstractmethod
    def command(tree: Path, selected: list[str], side: Side) -> list[str]:
        """The checker's arguments after `python -m`, run in the revision extracted to the tree."""

    @staticmethod
    def batches(selected: list[str]) -> list[list[str]]:
        """The files checked by one run of the checker."""
        return [selected]

    @staticmethod
    def extract(repo: Path, ref: str, scratch: Path, selected: list[str]) -> None:
        """Write the revision's files the checker reads."""
        subprocess.run(["tar", "-x", "-C", scratch], input=git(repo, "archive", ref), check=True,
                       timeout=60)

    @classmethod
    def findings(cls, repo: Path, ref: str, head: str, root: str, selected: list[str],
                 side: Side) -> tuple[Side, Counter[str]]:
        with tempfile.TemporaryDirectory(prefix="debt-ratchet-check-") as scratch:
            tree = Path(scratch)
            cls.extract(repo, ref, tree, selected)
            configuration = tree / "pyproject.toml"
            configuration.unlink(missing_ok=True)
            if git(repo, "ls-tree", "--name-only", head, "--", "pyproject.toml"):
                configuration.write_bytes(git(repo, "show", f"{head}:pyproject.toml"))
            result = subprocess.run([sys.executable, "-m", *cls.command(tree, selected, side)],
                                    cwd=tree, env=cls.environment(root, tree), capture_output=True,
                                    text=True, timeout=600)
        if result.returncode not in (0, 1):
            raise ValueError(f"{cls.__name__} could not check {ref}: {result.stderr}")
        # A checker reports each file under the path it was given, inside the tree or relative to
        # it; mypy also reports the modules it follows outside the tree, which are not measured.
        output = result.stdout + result.stderr
        reported = (Path(tree, match[1]) for match in cls.finding.finditer(output))
        return side, Counter(path.relative_to(tree).as_posix()
                             for path in reported if path.is_relative_to(tree))

    @classmethod
    def parts(cls, repo: Path, base: str, head: str, changed: set[str],
              root: str) -> tuple[Callable[[], object], ...]:
        return tuple(partial(cls.findings, repo, ref, head, root, batch, side)
                     for side, ref in zip(Side, (base, head), strict=True)
                     for batch in cls.batches(sorted(changed & python_paths(repo, ref, root)))
                     if batch)

    @classmethod
    def combine(cls, results: list, repo: Path, base: str, head: str, changed: set[str],
                root: str) -> tuple[dict[str, int], dict[str, int]]:
        found: dict[Side, Counter[str]] = {side: Counter() for side in Side}
        for side, counted in results:
            found[side] += counted
        before, after = ({f"{cls.__name__}:{path}": found[side][path] for path in sorted(changed)}
                         for side in Side)
        return before, after


class Unformatted(CheckedRevision):
    """1 when Black would rewrite the file: a formatted file stays formatted."""

    finding = re.compile(r"^would reformat (.+)$", re.MULTILINE)

    @staticmethod
    def batches(selected: list[str]) -> list[list[str]]:
        # One process per file runs the files at once without Black starting its own workers.
        return [[path] for path in selected]

    @staticmethod
    def extract(repo: Path, ref: str, scratch: Path, selected: list[str]) -> None:
        # Black reads only the file and the configuration.
        for path, source in GitMeasure.sources(repo, ref, set(selected)):
            (scratch / path).parent.mkdir(parents=True, exist_ok=True)
            (scratch / path).write_bytes(source)

    @staticmethod
    def command(tree: Path, selected: list[str], side: Side) -> list[str]:
        return ["black", "--check", *selected]


class LintFindings(CheckedRevision):
    """Ruff diagnostics under the selected rules."""

    finding = re.compile(r"^(.+?\.py):\d+:\d+: ", re.MULTILINE)

    @staticmethod
    def command(tree: Path, selected: list[str], side: Side) -> list[str]:
        return ["ruff", "check", "--no-cache", "--output-format", "concise", *selected]


class TypeErrors(CheckedRevision):
    """Mypy errors per changed file. Mypy checks the changed files and every module they import,
    so a file's count can change when a module it imports does."""

    finding = re.compile(r"^(.+?\.py):\d+: error: ", re.MULTILINE)

    @staticmethod
    def command(tree: Path, selected: list[str], side: Side) -> list[str]:
        # Mypy reuses a cached module whose path, size and whole-second mtime match its file, and
        # hashes the file otherwise. Every extracted tree has a new path, so each module is checked
        # against its content while unchanged analysis carries from one run to the next. Each side
        # keeps its own cache: the two revisions run at once, and a base usually matches the
        # previous run's base.
        cache = Path.home() / ".cache/agent-comms-debt-ratchet/mypy" / side.value
        return ["mypy", "--explicit-package-bases", "--cache-dir", str(cache), "--no-error-summary",
                *(str(tree / path) for path in selected)]

    @staticmethod
    def environment(root: str, tree: Path) -> dict[str, str]:
        """Module names start at the source directory, as they do when the package is installed.
        Tools import the same revision's ``src`` (``upgrade_bus.py`` puts it first), never the
        installed one."""
        paths = dict.fromkeys(str(tree / directory) for directory in (Path(root).parts[0], "src"))
        return {**os.environ, "MYPYPATH": os.pathsep.join((*paths, str(AUDIT_SCRIPTS)))}


@dataclass(frozen=True)
class ChangedSource:
    """One head module and the new-side lines changed by its Git patch."""

    path: str
    source: bytes
    lines: frozenset[int]

    @cached_property
    def tree(self) -> ast.Module:
        return ast.parse(self.source, self.path)

    @cached_property
    def tokens(self) -> tuple[tokenize.TokenInfo, ...]:
        return tuple(tokenize.tokenize(io.BytesIO(self.source).readline))

    @classmethod
    def read(cls, repo: Path, base: str, head: str, path: str) -> ChangedSource:
        patch = git(repo, "diff", "--no-renames", "--no-ext-diff", "--no-textconv",
                    "--no-color", "--unified=0", "--inter-hunk-context=0", base, head, "--", path)
        lines: set[int] = set()
        for header in patch.decode().splitlines():
            if header.startswith("@@ "):
                # Git owns this hunk header, not the inspected Python source.
                span = header.split()[2].removeprefix("+").split(",")
                start = int(span[0])
                count = int(span[1]) if len(span) == 2 else 1
                lines.update(range(start, start + count))
        return cls(path, git(repo, "show", f"{head}:{path}"), frozenset(lines))


class CleanChangedLines(OccurrenceMeasure):
    """No occurrence may be written, even when another occurrence is deleted."""

    @staticmethod
    def site_lines(node: ast.AST) -> range:
        return range(node.lineno, node.end_lineno + 1)

    @staticmethod
    def introduced(repo: Path, base: str, head: str, path: str) -> dict[str, list[int]]:
        """Each strict measure's sites on the path's changed lines."""
        source = ChangedSource.read(repo, base, head, path)
        return {f"{measure.__name__}:{path}": measure.changed_sites(source)
                for measure in Measure.members_with(CleanChangedLines)}

    @classmethod
    def changed_sites(cls, source: ChangedSource) -> list[int]:
        """The first changed line of each occurrence written on a changed line."""
        sites = []
        for node in cls.sites(source.tree, source.path):
            if ((count := cls.occurrences(node))
                    and (touched := source.lines.intersection(cls.site_lines(node)))):
                sites.extend([min(touched)] * count)
        return sites


class FrozenFieldWrite(CleanChangedLines, PerFileOccurrenceMeasure):
    @staticmethod
    def occurrences(node: ast.AST) -> int:
        match node:
            case ast.Call(func=ast.Attribute(value=ast.Name(id="object"), attr="__setattr__")):
                return 1
        return 0


class BannedVocabulary(CleanChangedLines, PerFileOccurrenceMeasure):
    words: typing.ClassVar[tuple[str, ...]] = ("admission", "admit", "custody", "receipt")

    @classmethod
    def occurrences(cls, node: ast.AST) -> int:
        match node:
            case (ast.Name(id=name) | ast.Attribute(attr=name)
                  | ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name)
                  | ast.ClassDef(name=name) | ast.arg(arg=name)
                  | ast.keyword(arg=str(name))):
                return int(any(word in name.casefold() for word in cls.words))
        return 0

    @staticmethod
    def site_lines(node: ast.AST) -> range:
        line = node.end_lineno if isinstance(node, ast.Attribute) else node.lineno
        return range(line, line + 1)

    @classmethod
    def prose_lines(cls, source: ChangedSource) -> frozenset[int]:
        docstrings: set[int] = set()
        for node in ast.walk(source.tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                match node.body:
                    case [ast.Expr(value=ast.Constant(value=str()) as value), *_]:
                        docstrings.update(range(value.lineno, value.end_lineno + 1))
        found: set[int] = set()
        for token in source.tokens:
            if token.type == tokenize.COMMENT or (
                token.type == tokenize.STRING and token.start[0] in docstrings
            ):
                for line, text in enumerate(token.string.splitlines(), token.start[0]):
                    if any(word in text.casefold() for word in cls.words):
                        found.add(line)
        return frozenset(found)

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        module = ChangedSource(filename, source, frozenset())
        return sum(cls.occurrences(node) for node in ast.walk(module.tree)) + len(cls.prose_lines(module))

    @classmethod
    def changed_sites(cls, source: ChangedSource) -> list[int]:
        return [*super().changed_sites(source), *(cls.prose_lines(source) & source.lines)]


@dataclass(frozen=True)
class DecoratorBinding:
    """The spellings one module binds to a declared decorator, read from its imports alone.

    A decorator of the same name declared or imported from anywhere else marks nothing.
    """

    names: frozenset[str]

    @classmethod
    def of(cls, decorator: typing.Callable[..., object], tree: ast.AST,
           filename: str) -> DecoratorBinding:
        owner, name = decorator.__module__, decorator.__name__
        parts = Path(filename).with_suffix("").parts
        module = parts[1:] if parts[:1] == ("src",) else parts
        package = module[:-1]
        names = {name} if ".".join(module) == owner else set()
        for node in ast.walk(tree):
            match node:
                case ast.Import(names=aliases):
                    names.update(f"{alias.asname or alias.name}.{name}"
                                 for alias in aliases if alias.name == owner)
                case ast.ImportFrom(module=source, names=aliases, level=level):
                    base = package[:len(package) - level + 1] if level else ()
                    resolved = ".".join((*base, source) if source else base)
                    for alias in aliases:
                        if f"{resolved}.{alias.name}" == owner:
                            names.add(f"{alias.asname or alias.name}.{name}")
                        if resolved == owner and alias.name == name:
                            names.add(alias.asname or name)
        return cls(frozenset(names))

    def marks(self, decorator: ast.expr) -> bool:
        return ast.unparse(decorator) in self.names


class NoneSpelling(MroDispatch):
    """One node's spellings of absence as None, never evaluated code."""

    def __init__(self) -> None:
        self.found = 0

    @staticmethod
    def absent(node: ast.AST) -> bool:
        match node:
            case ast.Constant(value=None):
                return True
        return False

    @handles(ast.Compare)
    def identity(self, node: ast.Compare) -> None:
        left = node.left
        for operator, right in zip(node.ops, node.comparators, strict=True):
            match operator:
                case ast.Is() | ast.IsNot():
                    self.found += self.absent(left) or self.absent(right)
            left = right

    @handles(ast.BinOp)
    def union(self, node: ast.BinOp) -> None:
        match node:
            case ast.BinOp(op=ast.BitOr(), left=left, right=right):
                self.found += self.absent(left) or self.absent(right)

    @handles(ast.Subscript)
    def typing_spelling(self, node: ast.Subscript) -> None:
        match node.value, node.slice:
            case ast.Name(id="Optional") | ast.Attribute(attr="Optional"), _:
                self.found += 1
            case ast.Name(id="Union") | ast.Attribute(attr="Union"), ast.Tuple(elts=members):
                self.found += any(self.absent(member) for member in members)

    @handles(ast.keyword)
    def default(self, node: ast.keyword) -> None:
        match node:
            case ast.keyword(arg="default", value=value):
                self.found += self.absent(value)


class NoneAbsence(CleanChangedLines, PerFileOccurrenceMeasure):
    """Absence spelled as None: ``is None`` tests, annotations that allow None, ``default=None``.

    Absence belongs to a declared null member that answers for itself. A decoder that
    meets None from outside declares itself with ``boundary_decoder``; its body is not counted.
    """

    @classmethod
    def sites(cls, tree: ast.AST, filename: str) -> Iterator[ast.AST]:
        marker = DecoratorBinding.of(boundary_decoder, tree, filename)
        pending = [tree]
        while pending:
            node = pending.pop()
            match node:
                case ast.FunctionDef(decorator_list=decorators) | ast.AsyncFunctionDef(
                        decorator_list=decorators) if any(map(marker.marks, decorators)):
                    continue
            yield node
            pending.extend(ast.iter_child_nodes(node))

    @staticmethod
    def occurrences(node: ast.AST) -> int:
        spelling = NoneSpelling()
        spelling.dispatch_sync(node)
        return spelling.found

class RawRecordShapes(PerFileMeasure):
    """Reuse the overlay's raw-shape detector for every production file."""

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        text = source.decode()
        module = AuditModule(filename, text, ast.parse(text, filename), filename)
        package = AuditPackage(AuditRepository(Path.cwd()), "", filename, (module,), ())
        return sum(1 for facts in package.functions[filename]
                   for _finding in RawShape.from_facts(facts, package))


class FunctionLocalImport(PerFileMeasure):
    """Imports executed inside a function, including nested local classes."""

    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0
            self.found = 0

        def generic_visit(self, node: ast.AST) -> None:
            nested = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            self.depth += nested
            if self.depth and isinstance(node, (ast.Import, ast.ImportFrom)):
                self.found += 1
            super().generic_visit(node)
            self.depth -= nested

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        collector = cls.Collector()
        collector.visit(ast.parse(source, filename))
        return collector.found


class IdentityReading:
    """How one literal string stands for an identity instead of a typed reference."""


class NamedDeclaration(IdentityReading):
    """The literal spells a class, module or function this repository declares."""


class ComparedKind(IdentityReading):
    """The literal is one arm of a kind decided by comparing spellings."""


class NamedAccess(IdentityReading):
    """The literal names the attribute, module or family member a call reaches by name."""


@dataclass(frozen=True)
class IdentitySite:
    line: int
    spelling: str
    reading: type[IdentityReading]
    target: str


@dataclass(frozen=True)
class ModuleDeclarations:
    """One module's top-level multi-word function names and its classes with their base names."""

    functions: tuple[str, ...]
    classes: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class DeclaredSpellings:
    """Every spelling a revision's production Python declares as a name.

    Classes count under their own name, src modules under their dotted path, and modules and
    top-level functions under a multi-word name; a one-word module or function name is also an
    ordinary word, so a literal equal to it is not taken as naming it. Enum members are the
    declared boundary spellings of another program's contract, so literals in an enum's body
    are the one place such a spelling may appear.
    """

    names: dict[str, str]
    enums: frozenset[str]

    boundary_enums: typing.ClassVar[frozenset[str]] = frozenset(
        kind.__name__ for kind in (enum.Enum, enum.StrEnum, enum.IntEnum, enum.Flag, enum.IntFlag))
    named_calls: typing.ClassVar[frozenset[str]] = frozenset(
        call.__name__ for call in (getattr, setattr, hasattr, delattr, __import__,
                                   importlib.import_module))
    selecting_calls: typing.ClassVar[frozenset[str]] = frozenset(
        call.__name__ for call in (DeclaredFamily.member_named, DeclaredFamily.field_value))
    kind_spelling: typing.ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z_][\w.-]*")
    package_marker: typing.ClassVar[str] = Path(importlib.__file__).name
    entry_guard: typing.ClassVar[str] = ast.dump(ast.parse('__name__ == "__main__"', mode="eval").body)

    @staticmethod
    def multiword(name: str) -> bool:
        return name.strip("_").count("_") > 0

    @staticmethod
    def dotted_name(module: Path, packages: set[Path]) -> str:
        """The import name of a module inside a package, from the package directories above it."""
        top = module.parent
        if top not in packages:
            return ""
        while top.parent in packages:
            top = top.parent
        parts = module.relative_to(top.parent).parts
        return ".".join(parts[:-1] if module.name == Path(DeclaredSpellings.package_marker).stem else parts)

    @classmethod
    def declarations(cls, path: str, source: bytes) -> ModuleDeclarations:
        tree = ast.parse(source, path)
        functions = (node.name for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
        classes = (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
        return ModuleDeclarations(
            tuple(filter(cls.multiword, functions)),
            tuple((node.name, tuple(base.id if isinstance(base, ast.Name) else base.attr
                                    for base in node.bases
                                    if isinstance(base, (ast.Name, ast.Attribute))))
                  for node in classes))

    @classmethod
    def read(cls, modules: dict[str, ModuleDeclarations]) -> DeclaredSpellings:
        """The spellings declared by the modules, taken in the dictionary's order."""
        names: dict[str, str] = {}
        bases: dict[str, set[str]] = {}
        packages = {Path(path).parent for path in modules if Path(path).name == cls.package_marker}
        for path, declared in modules.items():
            module = Path(path).with_suffix("")
            if cls.multiword(module.stem):
                names[module.stem] = f"module {path}"
            if dotted := cls.dotted_name(module, packages):
                names[dotted] = f"module {path}"
            for function in declared.functions:
                names.setdefault(function, f"function {path}:{function}")
            for name, parents in declared.classes:
                names[name] = f"class {path}:{name}"
                bases.setdefault(name, set()).update(parents)
        enums = set(cls.boundary_enums)
        while grown := {name for name, parents in bases.items() if parents & enums} - enums:
            enums |= grown
        return cls(names, frozenset(enums))

    def exempt(self, tree: ast.AST) -> set[int]:
        """Literals no reader takes as identity: prose, interpolation pieces, class-header
        options, enum boundary declarations and the interpreter's entry-point guard."""
        exempt: set[int] = set()
        for node in ast.walk(tree):
            match node:
                case ast.Module(body=[ast.Expr(value=ast.Constant() as prose), *_]) | ast.ClassDef(
                        body=[ast.Expr(value=ast.Constant() as prose), *_]) | ast.FunctionDef(
                        body=[ast.Expr(value=ast.Constant() as prose), *_]) | ast.AsyncFunctionDef(
                        body=[ast.Expr(value=ast.Constant() as prose), *_]):
                    exempt.add(id(prose))
            match node:
                case ast.JoinedStr():
                    exempt.update(map(id, ast.walk(node)))
                case ast.ClassDef(name=name) if name in self.enums:
                    exempt.update(map(id, ast.walk(node)))
                case ast.ClassDef(keywords=keywords):
                    exempt.update(id(part) for keyword in keywords for part in ast.walk(keyword))
                case ast.Compare() if ast.dump(node) == self.entry_guard:
                    exempt.update(map(id, ast.walk(node)))
        return exempt

    def sites(self, source: bytes, filename: str) -> list[IdentitySite]:
        tree = ast.parse(source, filename)
        exempt = self.exempt(tree)
        found: dict[int, IdentitySite] = {}

        def record(literal: ast.AST, reading: type[IdentityReading], target: str) -> None:
            if (isinstance(literal, ast.Constant) and isinstance(literal.value, str)
                    and id(literal) not in exempt):
                found.setdefault(id(literal), IdentitySite(literal.lineno, literal.value, reading, target))

        for node in ast.walk(tree):
            match node:
                case ast.Call(func=ast.Name(id=call) | ast.Attribute(attr=call), args=arguments):
                    if call in self.named_calls:
                        position = int(call not in {__import__.__name__, importlib.import_module.__name__})
                        for argument in arguments[position:position + 1]:
                            record(argument, NamedAccess, f"{call} by name")
                    if call in self.selecting_calls:
                        for argument in arguments:
                            record(argument, NamedAccess, f"family member selected by {call}")
                case ast.Compare(left=left, ops=operators, comparators=comparators):
                    for operator, right in zip(operators, comparators, strict=True):
                        match operator, right:
                            case ast.Eq() | ast.NotEq(), _:
                                for side in (left, right):
                                    if self.kind(side):
                                        record(side, ComparedKind, "kind compared by spelling")
                            case ast.In() | ast.NotIn(), ast.Set(elts=items) | ast.Tuple(
                                    elts=items) | ast.List(elts=items):
                                for item in filter(self.kind, items):
                                    record(item, ComparedKind, "kind compared by spelling")
                        left = right
                case ast.MatchValue(value=value) if self.kind(value):
                    record(value, ComparedKind, "kind matched by spelling")
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in self.names:
                record(node, NamedDeclaration, self.names[node.value])
        return sorted(found.values(), key=lambda site: site.line)

    def kind(self, node: ast.AST) -> bool:
        return (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and self.kind_spelling.fullmatch(node.value) is not None)


class PytestTestPaths:
    """Where pytest's configuration declares the test tree; pytest owns these spellings."""

    file = "pyproject.toml"
    table = ("tool", "pytest", "ini_options")
    key = "testpaths"

    @classmethod
    def at(cls, repo: Path, ref: str) -> tuple[str, ...]:
        """A revision without the configuration file declares no test tree."""
        if not git(repo, "ls-tree", "--name-only", ref, "--", cls.file).strip():
            return ()
        declared = tomllib.loads(git(repo, "show", f"{ref}:{cls.file}").decode())
        for section in cls.table:
            declared = declared.get(section, {})
        return tuple(declared.get(cls.key, ()))


@cache
def production_paths(repo: Path, ref: str) -> frozenset[str]:
    """Every Python path the revision tracks outside its declared test tree."""
    tests = PytestTestPaths.at(repo, ref)
    return frozenset(path for path in python_paths(repo, ref, ".")
                     if not any(Path(path).is_relative_to(tree) for tree in tests))


class StringIdentity(InventoryMeasure[ModuleDeclarations]):
    """Literal strings standing for something the code declares, per production file.

    A class, module or function named by its spelling, a kind decided by comparing spellings,
    and an attribute, module or family member reached by a literal name all bind by text, so a
    rename or a second writer of the same spelling breaks them silently. The spellings and the
    measured files are every production Python path, not only --root: the target of a spelling
    and its use can sit in any tree, as tools/cutover's step predecessors did.
    """

    @classmethod
    def inventory(cls, repo: Path, ref: str, root: str) -> frozenset[str]:
        return production_paths(repo, ref)

    reading = DeclaredSpellings.declarations

    @classmethod
    def measured(cls, repo: Path, ref: str, base: str, head: str,
                 readings: dict[str, ModuleDeclarations], root: str) -> dict[str, int]:
        spellings = DeclaredSpellings.read(readings)
        selected = readings.keys() & touched_paths(repo, base, head)
        return {f"{cls.__name__}:{path}": len(spellings.sites(source, path))
                for path, source in GitMeasure.sources(repo, ref, selected)}

    align = PerFileMeasure.align


class ForeignFieldSpellings(PerFileOccurrenceMeasure):
    """Literal identifier-spelling rosters used as loop inputs."""

    @staticmethod
    def occurrences(node: ast.AST) -> int:
        match node:
            case (ast.For(iter=ast.Tuple(elts=items))
                  | ast.AsyncFor(iter=ast.Tuple(elts=items))
                  | ast.comprehension(iter=ast.Tuple(elts=items))):
                return int(len(items) > 1 and all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str)
                    and item.value.isidentifier() for item in items
                ))
        return 0


class BroadExceptDefault(PerFileOccurrenceMeasure):
    broad_names: typing.ClassVar[frozenset[str]] = frozenset({"Exception", "BaseException"})

    @classmethod
    def broad(cls, node: ast.AST | None, *, names: frozenset[str] | None = None) -> bool:
        names = cls.broad_names if names is None else names
        match node:
            case None:
                return True
            case ast.Name(id=name) | ast.Attribute(attr=name):
                return name in names
            case ast.Tuple(elts=items):
                return any(cls.broad(item, names=names) for item in items)
        return False

    @staticmethod
    def default(node: ast.AST | None) -> bool:
        match node:
            case None | ast.Constant(value=None | False | 0 | ""):
                return True
            case ast.List(elts=[]) | ast.Tuple(elts=[]) | ast.Set(elts=[]) | ast.Dict(keys=[]):
                return True
        return False

    @classmethod
    def returns_default(cls, node: ast.AST) -> bool:
        match node:
            case ast.Return(value=value):
                return cls.default(value)
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef() | ast.Lambda():
                return False
        return any(cls.returns_default(child) for child in ast.iter_child_nodes(node))

    @classmethod
    def occurrences(cls, node: ast.AST) -> int:
        return int(isinstance(node, ast.ExceptHandler) and cls.broad(node.type)
                   and any(cls.returns_default(statement) for statement in node.body))


class BroadSwallow(CleanChangedLines, BroadExceptDefault):
    """A broad handler without a raise hides the failure, whatever it returns."""

    broad_names = frozenset({
        "Exception", "BaseException", "ValueError", "RuntimeError", "OSError", "TypeError",
        "KeyError", "AttributeError", "LookupError",
    })

    suppressed_names = broad_names | {"ConnectionError", "TimeoutError"}

    @classmethod
    def suppresses(cls, node: ast.AST) -> bool:
        match node:
            case ast.Call(func=ast.Name(id="suppress") | ast.Attribute(attr="suppress"), args=arguments):
                return any(cls.broad(argument, names=cls.suppressed_names) for argument in arguments)
        return False

    @classmethod
    def occurrences(cls, node: ast.AST) -> int:
        match node:
            case ast.With(items=items) | ast.AsyncWith(items=items):
                return sum(cls.suppresses(item.context_expr) for item in items)
            case ast.ExceptHandler(type=error):
                return int(cls.broad(error) and not any(
                    isinstance(child, ast.Raise) for child in ast.walk(node)))
        return 0


class FamilyFlattened(PerFileMeasure, ast.NodeTransformer):
    """Keep audit behavior checks, excluding owned identity and exposing codec detours."""

    def __init__(self) -> None:
        self.owners: list[str] = []
        self.round_trips = 0

    def visit_ClassDef(self, node: ast.ClassDef):
        self.owners.append(node.name)
        try:
            return self.generic_visit(node)
        finally:
            self.owners.pop()

    def visit_Attribute(self, node: ast.Attribute):
        if (node.attr == "declared_name" and isinstance(node.value, ast.Name)
                and node.value.id in {"cls", "self", *self.owners[-1:]}):
            return ast.copy_location(ast.Constant(value=None), node)
        return self.generic_visit(node)

    def visit_Module(self, node: ast.Module) -> ast.AST:
        # `python -m` runs a module through this guard; it names the interpreter, not a family.
        for statement in node.body:
            match statement:
                case ast.If(test=ast.Compare(left=ast.Name(id="__name__"), ops=[ast.Eq()],
                                             comparators=[ast.Constant(value="__main__")])):
                    statement.test = ast.copy_location(ast.Constant(value=None), statement.test)
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        match node:
            case ast.Call(
                func=ast.Attribute(value=ast.Name(id="FieldCodec"), attr="decode"),
                args=[ast.Name(id="str"), ast.Call(
                    func=ast.Attribute(value=ast.Name(id="FieldCodec"), attr="encode"),
                    args=[_], keywords=[],
                )], keywords=[],
            ):
                self.round_trips += 1
                return ast.copy_location(ast.Constant(value=None), node)
        return self.generic_visit(node)

    # GitMeasure supplies complete repository-relative paths. Only those exact
    # original modules own codec/schema behavior; nested package tails do not.
    mechanism_modules = frozenset(Path("src", package, module) for package, module in {
        ("agent_comms", "field_codec.py"), ("agent_comms", "typed_table.py"),
        ("agent_comms", "coordination_schema.py"),
        ("agent_comms", "coordinated_runtime_schema.py"),
        ("toad", "agent_schema.py"), ("toad", "db.py"),
    })

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        if Path(filename) in cls.mechanism_modules:
            return 0
        projection = cls()
        tree = projection.visit(ast.parse(source, filename))
        return AuditFamilyFlattened.count(tree) + projection.round_trips


class DispatchCases(MroDispatch):
    """Collect cases of one external Python AST without executing its source."""

    minimum_arms = 3

    def __init__(self) -> None:
        self.cases: dict[str, set[str]] = {}

    def add(self, subject: ast.AST, case: str) -> None:
        self.cases.setdefault(ast.dump(subject), set()).add(case)

    def groups(self) -> tuple[frozenset[str], ...]:
        return tuple(frozenset(cases) for cases in self.cases.values()
                     if len(cases) >= self.minimum_arms)

    def read_function(self, function: ast.AST) -> None:
        pending = list(ast.iter_child_nodes(function))
        while pending:
            node = pending.pop()
            # A nested scope's comparisons cannot combine with its parent's.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            self.dispatch_sync(node)
            pending.extend(ast.iter_child_nodes(node))


class LiteralDispatchCases(DispatchCases):
    @staticmethod
    def literal(node: ast.AST) -> bool:
        return (isinstance(node, ast.Constant)
                and isinstance(node.value, (str, int, float))
                and not isinstance(node.value, bool))

    @handles(ast.Compare)
    def comparison(self, node: ast.Compare) -> None:
        left = node.left
        for operator, right in zip(node.ops, node.comparators, strict=True):
            match operator, right:
                case (ast.Eq() | ast.NotEq(), literal) if self.literal(literal):
                    self.add(left, repr(literal.value))
                case (ast.In() | ast.NotIn(), ast.Set(elts=items) | ast.Tuple(elts=items) | ast.List(elts=items)):
                    for item in items:
                        if self.literal(item):
                            self.add(left, repr(item.value))
            left = right

    @handles(ast.Match)
    def match_cases(self, node: ast.Match) -> None:
        for case in node.cases:
            if isinstance(case.pattern, ast.MatchValue) and self.literal(case.pattern.value):
                self.add(node.subject, repr(case.pattern.value.value))


class TypeDispatchCases(DispatchCases):
    @handles(ast.Call)
    def instance_check(self, node: ast.Call) -> None:
        match node:
            case ast.Call(func=ast.Name(id="isinstance"), args=[subject, kind], keywords=[]):
                self.add(subject, ast.dump(kind))

    @handles(ast.Compare)
    def exact_type(self, node: ast.Compare) -> None:
        match node:
            case ast.Compare(left=ast.Call(func=ast.Name(id="type"), args=[subject]), comparators=[kind]):
                self.add(subject, ast.dump(kind))

    @handles(ast.Match)
    def match_cases(self, node: ast.Match) -> None:
        for case in node.cases:
            if isinstance(case.pattern, ast.MatchClass):
                self.add(node.subject, ast.dump(case.pattern.cls))


class DispatchMeasure(PerFileOccurrenceMeasure):
    """Screen three-arm subjects per function, with per-file growth isolation.

    These are candidates, not a proof that an external taxonomy is ours. Review
    ownership at each reported site; no exception roster or second registry.
    """

    @classmethod
    @abstractmethod
    def collector(cls) -> DispatchCases: ...

    @classmethod
    def groups(cls, node: ast.AST) -> tuple[frozenset[str], ...]:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ()
        collector = cls.collector()
        collector.read_function(node)
        return collector.groups()

    @classmethod
    def occurrences(cls, node: ast.AST) -> int:
        return len(cls.groups(node))


class StringDispatch(DispatchMeasure):
    @classmethod
    def collector(cls) -> DispatchCases:
        return LiteralDispatchCases()


class TypeSwitch(DispatchMeasure):
    @classmethod
    def collector(cls) -> DispatchCases:
        return TypeDispatchCases()


class DispatchArms:
    """Count growth inside an existing candidate through the same collector."""

    @classmethod
    def occurrences(cls, node: ast.AST) -> int:
        return sum(len(group) for group in cls.groups(node))


class StringDispatchArms(DispatchArms, StringDispatch):
    pass


class TypeSwitchArms(DispatchArms, TypeSwitch):
    pass


class BuiltinHandlerTypeSwitch(BuiltinHandlerDeclarations, PerFileMeasure):
    """Share the audit collector; per-file counts prevent moving primitive debt."""

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        if not cls.admits(filename):
            return 0
        return cls.count_module(ast.parse(source, filename))


class GodClassExcess(InventoryMeasure[dict[str, int]]):
    """Independent class lines beyond 500, including newly introduced classes."""

    threshold = 500

    @classmethod
    def inventory(cls, repo: Path, ref: str, root: str) -> frozenset[str]:
        # Complete inventories distinguish a moved declaration from another
        # same-named class in an unchanged module.
        return python_paths(repo, ref, root)

    @classmethod
    def measured(cls, repo: Path, ref: str, base: str, head: str,
                 readings: dict[str, dict[str, int]], root: str) -> dict[str, int]:
        return {identity: value
                for values in readings.values() for identity, value in values.items()}

    @classmethod
    def reading(cls, path: str, source: bytes) -> dict[str, int]:
        values = {}
        def visit(node: ast.AST, path: str, scope: tuple[str, ...]) -> None:
            match node:
                case ast.ClassDef(name=name, lineno=start, end_lineno=end, decorator_list=decorators):
                    scope = (*scope, name)
                    start = min([start, *(decorator.lineno for decorator in decorators)])
                    values[f"{cls.__name__}:{path}::{'.'.join(scope)}"] = max(0, end - start + 1 - cls.threshold)
                case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                    scope = (*scope, name)
            for child in ast.iter_child_nodes(node):
                visit(child, path, scope)
        visit(ast.parse(source, path), path, ())
        return values

    @classmethod
    def align(cls, base: dict[str, int], head: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
        def names(values):
            result = {}
            for identity in values:
                result.setdefault(identity.rsplit("::", 1)[1], []).append(identity)
            return result
        before, after = names(base), names(head)
        matched = {}
        consumed = set()
        for identity in head:
            if identity in base:
                matched[identity] = base[identity]
                consumed.add(identity)
                continue
            name = identity.rsplit("::", 1)[1]
            previous = before.get(name, ())
            if len(previous) == 1 and len(after[name]) == 1:
                matched[identity] = base[previous[0]]
                consumed.add(previous[0])
            else:
                # A new declaration must not introduce any god-class excess.
                matched[identity] = 0
        current = dict(head)
        for identity in base.keys() - consumed:
            matched[identity] = base[identity]
            current[identity] = 0
        return matched, current


class TypeIdentity(OccurrenceMeasure):
    @staticmethod
    def is_type_call(node: ast.AST) -> bool:
        match node:
            case ast.Call(func=ast.Name(id="type"), args=[_], keywords=[]):
                return True
            case _:
                return False

    @staticmethod
    def occurrences(node: ast.AST) -> int:
        if not isinstance(node, ast.Compare):
            return 0
        return sum(
            isinstance(operator, (ast.Is, ast.IsNot))
            and (TypeIdentity.is_type_call(left) or TypeIdentity.is_type_call(right))
            for left, operator, right in zip(
                [node.left, *node.comparators[:-1]], node.ops, node.comparators, strict=True
            )
        )


class LongBooleanChain(OccurrenceMeasure):
    minimum_terms = 4

    @staticmethod
    def occurrences(node: ast.AST) -> int:
        match node:
            case ast.BoolOp(values=values):
                return int(len(values) >= LongBooleanChain.minimum_terms)
        return 0


class BooleanChainTerms(PerFileOccurrenceMeasure):
    @staticmethod
    def occurrences(node: ast.AST) -> int:
        match node:
            case ast.BoolOp(values=values) if len(values) >= LongBooleanChain.minimum_terms:
                return len(values)
        return 0


class ForeignAbsenceProbe(PerFileOccurrenceMeasure):
    """Use the audit owner's explicit absence checks, not arbitrary truthiness."""

    occurrences = AuditForeignAbsenceProbe.count


class CodecSubclass(PerFileOccurrenceMeasure):
    """TIME-9 census measure; the sealed FieldCodec guard also resolves aliases."""

    @staticmethod
    def codec_base(node: ast.AST) -> bool:
        match node:
            case ast.Name(id=name) | ast.Attribute(attr=name):
                return name.endswith("Codec")
        return False

    @staticmethod
    def occurrences(node: ast.AST) -> int:
        match node:
            case ast.ClassDef(bases=bases):
                return int(any(CodecSubclass.codec_base(base) for base in bases))
        return 0


class TypingImports(MroDispatch):
    """Resolve unshadowed module imports from the actual stdlib declarations.

    This bounded syntactic measure never executes inspected source. Nested
    bindings conservatively disqualify a name rather than inferring its flow.
    """

    def __init__(self, declarations: set[ast.AST]) -> None:
        self.declarations = declarations
        self.values: dict[str, object] = {}
        self.shadowed: set[str] = set()

    def bind(self, node: ast.AST, name: str, value: object) -> None:
        if node not in self.declarations or name in self.values:
            self.shadowed.add(name)
        self.values[name] = value

    @handles(ast.Import)
    def modules(self, node: ast.Import) -> None:
        for item in node.names:
            self.bind(node, item.asname or item.name.split(".")[0],
                      typing if item.name == typing.__name__ else None)

    @handles(ast.ImportFrom)
    def members(self, node: ast.ImportFrom) -> None:
        for item in node.names:
            declaration = None
            if node.level == 0 and node.module == typing.__name__:
                declaration = vars(typing).get(item.name)
            self.bind(node, item.asname or item.name, declaration)

    @handles(ast.Name)
    def assignment(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.shadowed.add(node.id)

    @handles(ast.Attribute)
    def mutation(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            for descendant in ast.walk(node.value):
                if isinstance(descendant, ast.Name):
                    self.shadowed.add(descendant.id)

    @handles(ast.arg)
    def parameter(self, node: ast.arg) -> None:
        self.shadowed.add(node.arg)

    @handles(ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    def named_binding(self, node) -> None:
        self.shadowed.add(node.name)

    @handles(ast.ExceptHandler)
    def exception_binding(self, node: ast.ExceptHandler) -> None:
        name = node.name
        if name is not None:
            self.shadowed.add(name)

    # Honor the package's Python 3.11 contract without inventing parser nodes.
    if hasattr(ast, "type_param"):
        @handles(ast.type_param)
        def type_parameter(self, node) -> None:
            self.shadowed.add(node.name)

    @classmethod
    def of(cls, tree: ast.Module) -> dict[str, object]:
        imports = cls(set(tree.body))
        for node in ast.walk(tree):
            imports.dispatch_sync(node)
        if "*" in imports.values:
            return {}  # A star import can replace any otherwise known binding.
        return {name: value for name, value in imports.values.items()
                if name not in imports.shadowed}

    @classmethod
    def at_any_depth(cls, tree: ast.Module) -> dict[str, object]:
        """Every name an import binds to ``typing``, wherever the import is written.

        For a measure that counts uses of a typing name: skipping an import inside
        ``if TYPE_CHECKING:`` or a function body would hide the uses it enables.
        """
        imports = cls(set(ast.walk(tree)))
        for node in ast.walk(tree):
            imports.dispatch_sync(node)
        return {name: value for name, value in imports.values.items() if value is not None}


class TypingReference(MroDispatch):
    """One AST expression's resolved imported declaration, never evaluated code."""

    def __init__(self, imports: dict[str, object]) -> None:
        self.imports = imports
        self.value: object = None

    @handles(ast.Name)
    def name(self, node: ast.Name) -> None:
        self.value = self.imports.get(node.id)

    @handles(ast.Attribute)
    def member(self, node: ast.Attribute) -> None:
        parent = TypingReference(self.imports)
        parent.dispatch_sync(node.value)
        if parent.value is typing:
            self.value = vars(typing).get(node.attr)

    def raw_access_count(self) -> int:
        """Typing declarations contribute no raw-record operation; unknowns do."""
        return int(getattr(self.value, "__module__", None) != typing.__name__)


class StringSubscript(OccurrenceMeasure):
    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        tree = ast.parse(source, filename)
        imports = TypingImports.of(tree)
        count = 0
        for node in ast.walk(tree):
            if cls.occurrences(node):
                reference = TypingReference(imports)
                reference.dispatch_sync(node.value)
                count += reference.raw_access_count()
        return count

    @staticmethod
    def occurrences(node: ast.AST) -> int:
        return int(
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        )


class AnyInTypeExpression(MroDispatch):
    """Count ``typing.Any`` in one type expression without evaluating it.

    Only type-forming syntax is followed: a subscript, a union, a member list
    and, in an annotation, a quoted forward reference. A call or a comparison
    is a runtime value, so ``annotation is Any`` in introspection code is not a use.
    """

    def __init__(self, imports: dict[str, object], quoted: bool) -> None:
        self.imports = imports
        self.quoted = quoted
        self.found = 0

    def resolved(self, node: ast.AST) -> object:
        reference = TypingReference(self.imports)
        reference.dispatch_sync(node)
        return reference.value

    @handles(ast.Name, ast.Attribute)
    def reference(self, node: ast.expr) -> None:
        self.found += int(self.resolved(node) is typing.Any)

    @handles(ast.Subscript)
    def parameterised(self, node: ast.Subscript) -> None:
        origin = self.resolved(node.value)
        members = [node.slice]
        if origin is typing.Literal:
            members = []  # Literal members are values, not types.
        if origin is typing.Annotated:
            members = getattr(node.slice, "elts", members)[:1]  # The rest is metadata.
        for member in members:
            self.dispatch_sync(member)

    @handles(ast.Tuple, ast.List)
    def members(self, node: ast.Tuple | ast.List) -> None:
        for member in node.elts:
            self.dispatch_sync(member)

    @handles(ast.BinOp)
    def union(self, node: ast.BinOp) -> None:
        self.dispatch_sync(node.left)
        self.dispatch_sync(node.right)

    @handles(ast.Constant)
    def forward_reference(self, node: ast.Constant) -> None:
        if not self.quoted or not isinstance(node.value, str):
            return
        try:
            quoted = ast.parse(node.value.strip(), mode="eval")
        except (SyntaxError, ValueError):
            return
        self.dispatch_sync(quoted.body)


@dataclass(frozen=True)
class AnyUse:
    """One written ``typing.Any`` where a file declares a type."""

    line: int
    site: str
    owner: str


class DeclaredTypeSites(MroDispatch):
    """Every place a file declares a type; each handler names one kind of place."""

    def __init__(self, imports: dict[str, object]) -> None:
        self.imports = imports
        self.uses: list[AnyUse] = []

    def read(self, tree: ast.Module) -> None:
        pending: list[tuple[ast.AST, tuple[ast.AST, ...]]] = [(tree, ())]
        while pending:
            node, scope = pending.pop()
            self.dispatch_sync(node, scope)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = (*scope, node)
            pending.extend((child, scope) for child in ast.iter_child_nodes(node))

    def declare(self, site: str, expression: ast.AST | None, scope: tuple[ast.AST, ...],
                quoted: bool = True) -> None:
        if expression is None:
            return
        counted = AnyInTypeExpression(self.imports, quoted)
        counted.dispatch_sync(expression)
        owner = ".".join(enclosing.name for enclosing in scope)
        self.uses.extend([AnyUse(expression.lineno, site, owner)] * counted.found)

    @staticmethod
    def record_class(scope: tuple[ast.AST, ...]) -> bool:
        """Whether the innermost scope is a class whose annotations are its fields."""
        def last_name(expression: ast.AST) -> str:
            return ast.unparse(getattr(expression, "func", expression)).rpartition(".")[2]
        decorators = {last_name(decorator) for decorator in scope[-1].decorator_list}
        bases = {last_name(base) for base in scope[-1].bases}
        return "dataclass" in decorators or bool(bases & {"NamedTuple", "TypedDict"})

    def assignment_site(self, scope: tuple[ast.AST, ...]) -> str:
        if not scope or not isinstance(scope[-1], ast.ClassDef):
            return "variable"
        return "dataclass field" if self.record_class(scope) else "class attribute"

    @handles(ast.arg)
    def parameter(self, node: ast.arg, scope: tuple[ast.AST, ...]) -> None:
        self.declare("parameter", node.annotation, scope)

    @handles(ast.FunctionDef, ast.AsyncFunctionDef)
    def result(self, node, scope: tuple[ast.AST, ...]) -> None:
        self.declare("return", node.returns, (*scope, node))

    @handles(ast.AnnAssign)
    def annotated(self, node: ast.AnnAssign, scope: tuple[ast.AST, ...]) -> None:
        self.declare(self.assignment_site(scope), node.annotation, scope)
        alias = AnyInTypeExpression(self.imports, quoted=True).resolved(node.annotation)
        if alias is typing.TypeAlias:
            self.declare("alias", node.value, scope)

    @handles(ast.Assign)
    def alias(self, node: ast.Assign, scope: tuple[ast.AST, ...]) -> None:
        # ``Payload = dict[str, Any]`` names a type. A bare member list such as
        # ``(Any, object)`` is a runtime collection, and runtime strings are not
        # forward references.
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            self.declare("alias", node.value, scope, quoted=False)

    # Honor the package's Python 3.11 contract without inventing parser nodes.
    if hasattr(ast, "TypeAlias"):
        @handles(ast.TypeAlias)
        def alias_statement(self, node, scope: tuple[ast.AST, ...]) -> None:
            self.declare("alias", node.value, scope)


class AnyAnnotation(PerFileMeasure):
    """``typing.Any`` written where a type is declared.

    A declared type is what a consumer turns into a form control, a parameter
    description or an array conversion; ``Any`` is the answer that is missing.
    Counted: parameters, returns, annotated assignments and type aliases,
    including nested (``dict[str, Any]``) and quoted uses.
    """

    @classmethod
    def uses(cls, source: bytes, filename: str) -> tuple[AnyUse, ...]:
        tree = ast.parse(source, filename)
        sites = DeclaredTypeSites(TypingImports.at_any_depth(tree))
        sites.read(tree)
        return tuple(sorted(sites.uses, key=lambda use: (use.line, use.site, use.owner)))

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        return len(cls.uses(source, filename))



class EditedCarry(ChangeMeasure):
    """Carry steps edited, renamed, or added under a deleted step's name.

    A bus that already ran a step never runs its edit, and a bus that had not
    yet run the original loses it; a deleted step's name still names that step
    in every ledger that recorded it. A different change is a new step.
    """

    @staticmethod
    def conversion(source: bytes) -> str:
        """Conversion code is immutable; publication backups are metadata, as was the
        predecessor an ``after`` string once restated."""
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                node.body = [statement for statement in node.body
                             if not (isinstance(statement, ast.FunctionDef)
                                     and statement.name == 'backup_files')
                             and not (isinstance(statement, ast.Assign)
                                      and [getattr(target, "id", None)
                                           for target in statement.targets] == ["after"])]
        return ast.dump(tree, include_attributes=False)

    @classmethod
    def counted(cls, repo: Path, base: str, head: str, changed: set[str], root: str):
        fields = iter(git(repo, "diff", "--name-status", "--find-renames=50%", "-z",
                          base, head, "--", str(STEPS)).decode().split("\0")[:-1])
        @cache
        def parents() -> dict[str, type[Carry]]:
            return carry_parents(git(repo, "show", f"{base}:{STEPS}/carry.py"))

        edited = 0
        for status in fields:
            path = next(fields)
            if status[0] in "RC":
                next(fields)
            name = Path(path).stem
            if not (name.startswith("carry_") and path.endswith(".py")):
                continue
            if status[0] in "MRC" or status == "T":
                before = git(repo, "show", f"{base}:{path}")
                if Step.holds_carry(before, parents()):
                    edited += (status != "M" or cls.conversion(before) !=
                               cls.conversion(git(repo, "show", f"{head}:{path}")))
            elif status == "A":
                edited += bool(git(repo, "log", "-1", "--format=%H", "--diff-filter=D", base, "--", path).strip())
        return {cls.__name__: 0}, {cls.__name__: edited}

class UncarriedRecordFormat(ChangeMeasure):
    """A stored record's format changed without its version, or its version without a carry.

    Files in the old format stay on disk, so a change to a versioned record's stored form
    bumps its version, and a bumped version arrives with a new carry step that rewrites the
    old files. The stored form is what FieldCodec writes, not the annotation text: two
    representations with the same wire spelling are the same format.
    """

    probe: typing.ClassVar[str] = (
        "import sys; sys.path[:0] = sys.argv[1:3]; "
        "import stored_formats; stored_formats.main(sys.argv[3:])"
    )

    @staticmethod
    def declares_version(source: bytes) -> bool:
        """Only a module with a class declaring `version` can hold a versioned record; others
        are never imported."""
        for node in ast.walk(ast.parse(source)):
            for item in node.body if isinstance(node, ast.ClassDef) else ():
                match item:
                    case ast.AnnAssign(target=ast.Name(id="version")) | ast.Assign(
                            targets=[ast.Name(id="version")]):
                        return True
        return False

    @classmethod
    def formats(cls, repo: Path, ref: str, root: str, changed: set[str]) -> dict[str, StoredForm]:
        """Each versioned record in the changed modules, measured by that revision's own code."""
        package = Path(root)
        while git(repo, "ls-tree", "--name-only", ref, "--", str(package / "__init__.py")):
            package = package.parent
        modules = [".".join(Path(path).relative_to(package).with_suffix("").parts)
                   for path in sorted(changed & python_paths(repo, ref, root))
                   if cls.declares_version(git(repo, "show", f"{ref}:{path}"))]
        if not modules:
            return {}
        with tempfile.TemporaryDirectory(prefix="debt-ratchet-formats-") as scratch:
            source = git(repo, "archive", ref, "--", str(package))
            subprocess.run(["tar", "-x", "-C", scratch], input=source, check=True, timeout=60)
            # Extracted source is stamped with its commit the way a build stamps it.
            for stamp in Path(scratch).rglob("package_stamp.py"):
                WheelStamp(revision(repo, ref)).write(stamp)
            result = subprocess.run([sys.executable, "-c", cls.probe,
                                     str(Path(scratch) / package), str(Path(__file__).resolve().parent),
                                     *modules], cwd=scratch,
                                    env=cls.environment(root, Path(scratch)),
                                    capture_output=True, text=True, check=True, timeout=300)
        return {name: StoredForm(**form) for name, form in json.loads(result.stdout).items()}

    @classmethod
    def counted(cls, repo: Path, base: str, head: str, changed: set[str], root: str):
        added = git(repo, "diff", "--name-only", "-z", "--diff-filter=A", base, head, "--", str(STEPS))
        carried = any(Path(path).stem.startswith("carry_") and path.endswith(".py")
                      for path in added.decode().split("\0"))
        before = cls.formats(repo, base, root, changed)
        after = cls.formats(repo, head, root, changed)
        uncarried = sum(before[name].version == form.version or not carried
                        for name, form in after.items() if name in before and before[name] != form)
        return {cls.__name__: 0}, {cls.__name__: uncarried}

def git(repo: Path, *args: str, input: bytes = b"") -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], input=input, timeout=20)


def revision(repo: Path, ref: str) -> str:
    return (
        git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    )


# Listings are read once per process: the measures pass commit identities, whose trees never change.
@cache
def python_paths(repo: Path, ref: str, root: str) -> frozenset[str]:
    return frozenset(
        path.decode()
        for path in git(repo, "ls-tree", "-rz", "--name-only", ref, "--", root).split(b"\0")
        if path.endswith(b".py")
    )


@cache
def touched_paths(repo: Path, base: str, head: str) -> frozenset[str]:
    # Disable rename detection: both sides of a move enter the union, including
    # moves into/out of the declared production source boundary.
    return frozenset(
        path.decode()
        for path in git(repo, "diff", "--no-renames", "--name-only", "-z", base, head, "--").split(b"\0")
    )


@dataclass(frozen=True)
class Comparison:
    root: str
    base_revision: str
    head_revision: str
    paths: tuple[str, ...]
    base: dict[str, int]
    head: dict[str, int]
    introduced: dict[str, int]
    introduced_lines: dict[str, list[int]]

    @projected(view="report")
    def delta(self) -> dict[str, int]:
        return {name: value - self.base[name] for name, value in self.head.items()}

    @property
    def failed(self) -> bool:
        return (any(measure.blocks(self.delta) for measure in Measure.members_with(Measure))
                or any(self.introduced.values()))


def compare(repo: Path, base: str, head: str, root: str) -> Comparison:
    source_root = Path(root)
    if source_root.is_absolute() or ".." in source_root.parts or not source_root.parts:
        raise ValueError("--root must be a repository-relative source directory")
    root = source_root.as_posix().rstrip("/") + "/"
    base, head = revision(repo, base), revision(repo, head)
    touched = touched_paths(repo, base, head)
    paths = set(touched & (python_paths(repo, base, root) | python_paths(repo, head, root)))
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    introduced: dict[str, int] = {}
    introduced_lines: dict[str, list[int]] = {}
    # Every measure reads production Python paths only, so with none touched each would
    # report no change.
    if not touched & (production_paths(repo, base) | production_paths(repo, head)):
        return Comparison(root, base, head, (), before, after, introduced, introduced_lines)

    measures = Measure.members_with(Measure)
    # Forked workers start from this already-imported module instead of importing it again.
    with ProcessPoolExecutor(mp_context=multiprocessing.get_context("fork")) as pool:
        parts = {measure: [pool.submit(part)
                           for part in measure.parts(repo, base, head, paths, root)]
                 for measure in measures}
        sites = [pool.submit(CleanChangedLines.introduced, repo, base, head, path)
                 for path in sorted(paths & python_paths(repo, head, root))]
        # Each measure combines as soon as its parts finish, while slower checkers still run.
        measured = {}
        owners = {future: measure for measure, futures in parts.items() for future in futures}
        for measure in measures:
            if not parts[measure]:
                measured[measure] = measure.combine([], repo, base, head, paths, root)
        for future in as_completed(owners):
            measure = owners[future]
            if measure not in measured and all(part.done() for part in parts[measure]):
                measured[measure] = measure.combine([part.result() for part in parts[measure]],
                                                    repo, base, head, paths, root)
        for measure in measures:
            measured_base, measured_head = measured[measure]
            before.update(measured_base)
            after.update(measured_head)
        for path_sites in sites:
            for identity, found in path_sites.result().items():
                introduced[identity] = len(found)
                if found:
                    introduced_lines[identity] = sorted(found)
    return Comparison(root, base, head, tuple(sorted(paths)), before, after,
                      introduced, introduced_lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Repository-relative production source directory")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    result = compare(Path.cwd(), args.base, args.head, args.root)
    print(json.dumps(FieldCodec.project(result, "report"), indent=2))
    return int(result.failed)


if __name__ == "__main__":
    raise SystemExit(main())
