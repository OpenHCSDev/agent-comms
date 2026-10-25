#!/usr/bin/env python3
"""Compare AST debt in every changed production Python path at two Git revisions."""

from __future__ import annotations

import argparse
import ast
import dataclasses
import enum
import hashlib
import importlib
import importlib.metadata
import inspect
import importlib.resources
import importlib.util
import io
import itertools
import json
import os
import pickle
import re
import subprocess
import sys
import tempfile
import time
import tokenize
import tomllib
import types
import typing
from typing import Annotated
from abc import abstractmethod
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import as_completed
from dataclasses import dataclass
from functools import cache, cached_property, lru_cache, partial
from pathlib import Path

from agent_comms.pinned_audit import audit
from audit.findings import Package as AuditPackage, ParsedModule as AuditModule, RawShape
from audit.repository import Repository as AuditRepository
from audit.handler_declarations import BuiltinHandlerDeclarations
from audit.measures import FamilyFlattened as AuditFamilyFlattened
from audit.measures import ForeignAbsenceProbe as AuditForeignAbsenceProbe

import agent_comms
from agent_comms import coordination_schema, field_codec, typed_table
from agent_comms.child_process import ForkedProcessPool, SpawnedProcess
from agent_comms.core_stamp import WheelStamp
from agent_comms.declared_family import DeclaredFamily
from agent_comms.installer.carry import STEPS
from stored_formats import StoredForm
from typing_names import TypingImports, TypingReference
from agent_comms.field_codec import FieldCodec, boundary_decoder, projected
from agent_comms.mro_dispatch import MroDispatch, handles

AUDIT_SCRIPTS = Path(audit.__path__[0]).parent
REPOSITORY = Path(__file__).resolve().parents[1]


class ContentCache:
    """Results that are pure functions of content, stored under a digest of that content, of
    this ratchet's own code and of the checkers it runs. The key is the content, so an entry
    never goes stale: any change to what produced it is a different key."""

    directory: typing.ClassVar[Path] = Path.home() / ".cache/agent-comms/debt-ratchet"
    checkers: typing.ClassVar[tuple[str, ...]] = ("black", "ruff", "mypy")
    retention_days: typing.ClassVar[int] = 14

    @staticmethod
    def sources() -> tuple[Path, ...]:
        """Every module loaded in this process under this repository's tools/ or src/, plus the
        audit package: measuring subprocesses import it from AUDIT_SCRIPTS, so sys.modules
        alone would miss a change to it. Sorted."""
        roots = (REPOSITORY / "tools", REPOSITORY / "src")
        origins = (Path(origin).resolve() for module in list(sys.modules.values())
                   if isinstance(module, types.ModuleType)
                   and (origin := module.__dict__.get("__file__")))
        loaded = {path for path in origins if any(path.is_relative_to(root) for root in roots)}
        audited = {path.resolve() for path in (AUDIT_SCRIPTS / "audit").rglob("*.py")}
        return tuple(sorted(loaded | audited))

    @staticmethod
    @cache
    def code() -> bytes:
        """This ratchet's measuring code (see sources), and each checker's version. Computed
        once, at the first key, after the measures' imports, so the set is the one that ran."""
        digest = hashlib.sha256(f"{sys.version}\0{__name__}".encode())
        for name in ContentCache.checkers:
            digest.update(f"\0{name}={importlib.metadata.version(name)}".encode())
        for path in ContentCache.sources():
            digest.update(b"\0" + str(path).encode() + b"\0" + path.read_bytes())
        return digest.digest()

    @classmethod
    def path(cls, *parts: str) -> Path:
        key = hashlib.sha256(b"\0".join((cls.code(), *(part.encode() for part in parts))))
        identity = key.hexdigest()
        return cls.directory / identity[:2] / identity

    @classmethod
    def stored(cls, *parts: str) -> tuple[object, ...]:
        """The stored result for these parts, as a one-item tuple, or an empty one."""
        path = cls.path(*parts)
        if not path.is_file():
            return ()
        os.utime(path)  # the mtime records the last use, which is what pruning reads
        return (pickle.loads(path.read_bytes()),)

    @classmethod
    def prune(cls) -> None:
        """Delete the entries neither written nor read for retention_days."""
        horizon = time.time() - cls.retention_days * 86400
        for path in cls.directory.glob("*/*"):
            if path.stat().st_mtime < horizon:
                path.unlink(missing_ok=True)

    @classmethod
    def store[Value](cls, value: Value, *parts: str) -> Value:
        path = cls.path(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
            staged.write(pickle.dumps(value))
        os.replace(staged.name, path)
        return value

    @classmethod
    def memo[Value](cls, compute: Callable[[], Value], *parts: str) -> Value:
        for value in cls.stored(*parts):
            return typing.cast(Value, value)
        return cls.store(compute(), *parts)



class Side(enum.Enum):
    """The two revisions a comparison measures; the value names a checker's cache."""

    BASE = "base"
    HEAD = "head"


class Measure(DeclaredFamily, affix="Measure"):
    @classmethod
    def key(cls, subject: str) -> str:
        """This measure's report key for one measured subject (a path, or a class in one)."""
        return f"{cls.__name__}:{subject}"

    @classmethod
    def owns(cls, key: str) -> bool:
        """Whether a report key is one of this measure's."""
        return key.partition(":")[0] == cls.__name__

    @classmethod
    def blocks(cls, delta: dict[str, int]) -> bool:
        return any(value > 0 for key, value in delta.items() if cls.owns(key))

    @classmethod
    def grew(cls, key: str, grown: int) -> str:
        """The line reporting one key's growth."""
        return f"grew {key} +{grown}"

    @classmethod
    def run_by(cls) -> type[Measure]:
        """The measure a comparison runs to take this one's measurements."""
        return cls

    @staticmethod
    def environment(root: str, tree: Path) -> dict[str, str]:
        """The environment of a tool run in a revision extracted to the tree."""
        return dict(os.environ)

    @staticmethod
    def source_paths(root: str, tree: Path) -> tuple[str, ...]:
        """Where a revision extracted to the tree imports from: the measured root's directory,
        then its ``src`` (``agent-comms install`` runs tools with it), never the installed one,
        then the pinned audit's scripts, a submodule no ``git archive`` of the tree carries."""
        return (*dict.fromkeys(str(tree / directory) for directory in (Path(root).parts[0], "src")),
                str(AUDIT_SCRIPTS))

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
    def context(cls, repo: Path, ref: str) -> str:
        """What a file's reading depends on besides the file itself."""
        return ""

    @classmethod
    def read(cls, repo: Path, ref: str, paths: list[str]) -> dict[str, Reading]:
        """Each file's reading, computed once per blob and context."""
        identities = GitMeasure.blobs(repo, ref, set(paths))
        context = cls.context(repo, ref)
        parts = {path: (cls.__name__, context, path, identities[path]) for path in paths}
        readings = {path: typing.cast(Reading, stored)
                    for path in paths for stored in ContentCache.stored(*parts[path])}
        fresh = cls.read_fresh(repo, ref, sorted(set(paths) - readings.keys()))
        return {**readings, **{path: ContentCache.store(reading, *parts[path])
                               for path, reading in fresh.items()}}

    @classmethod
    def read_fresh(cls, repo: Path, ref: str, paths: list[str]) -> dict[str, Reading]:
        return {path: cls.reading(path, source)
                for path, source in GitMeasure.sources(repo, ref, set(paths))}

    @classmethod
    def parts(cls, repo: Path, base: str, head: str, changed: set[str],
              root: str) -> tuple[Callable[[], object], ...]:
        before, after = cls.inventory(repo, base, root), cls.inventory(repo, head, root)
        shared = sorted((before & after) - ChangedPaths.of(repo, base, head).touched)
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


class GitMeasure(Measure):
    """Shared Git collection for declaration-owned snapshots."""

    @staticmethod
    def blobs(repo: Path, ref: str, selected: set[str]) -> dict[str, str]:
        """Each selected path's blob identity at the revision."""
        if not selected:
            return {}
        tree = git(repo, "--literal-pathspecs", "ls-tree", "-rz", ref, "--", *sorted(selected))
        identities = {}
        for entry in tree.split(b"\0"):
            if entry:
                metadata, path = entry.split(b"\t", 1)
                _, kind, identity = metadata.split()
                if kind != b"blob":
                    raise ValueError(f"Expected a source blob at {path!r}")
                identities[path.decode()] = identity.decode()
        if identities.keys() != selected:
            raise ValueError("Git omitted a selected path")
        return identities

    @staticmethod
    def sources(repo: Path, ref: str, selected: set[str]) -> list[tuple[str, bytes]]:
        """Read the selected Git blobs with one batch command."""
        if not selected:
            return []
        identities = {path: identity.encode()
                      for path, identity in GitMeasure.blobs(repo, ref, selected).items()}
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


class CountedSource(GitMeasure):
    """A measure that is one number per source file: a pass over the tree SyntaxPasses parses
    once for every pass. Passes read the shared tree and never write it."""

    @classmethod
    @abstractmethod
    def count(cls, source: bytes, filename: str) -> int:
        """Return this file's count."""

    @classmethod
    @abstractmethod
    def tally(cls, counts: list[tuple[str, int]]) -> dict[str, int]:
        """A revision's measurements from each selected file's count, in path order."""

    @classmethod
    def snapshot(cls, sources: list[tuple[str, bytes]]) -> dict[str, int]:
        return cls.tally([(path, cls.count(source, path)) for path, source in sources])

    @staticmethod
    @lru_cache(maxsize=4)
    def tree(source: bytes, filename: str) -> ast.Module:
        """The file's syntax, parsed once while every pass counts it."""
        return ast.parse(source, filename)

    @classmethod
    def run_by(cls) -> type[Measure]:
        return SyntaxPasses

    @classmethod
    def parts(cls, repo: Path, base: str, head: str, changed: set[str],
              root: str) -> tuple[Callable[[], object], ...]:
        return SyntaxPasses.parts(repo, base, head, changed, root)

    @classmethod
    def combine(cls, results: list, repo: Path, base: str, head: str, changed: set[str],
                root: str) -> tuple[dict[str, int], dict[str, int]]:
        before, after = SyntaxPasses.combine(results, repo, base, head, changed, root)
        return ({key: value for key, value in before.items() if cls.owns(key)},
                {key: value for key, value in after.items() if cls.owns(key)})


@dataclass(frozen=True)
class Site:
    """One counted occurrence: the file and line a grown measure points at."""

    path: str
    line: int


class LocatedSource(CountedSource):
    """A pass that knows the line of every occurrence it counts; its count is how many."""

    @classmethod
    @abstractmethod
    def lines(cls, source: bytes, filename: str) -> list[int]:
        """The line of each occurrence, once per unit counted."""

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        return len(cls.lines(source, filename))

    @classmethod
    def located(cls, sources: list[tuple[str, bytes]]) -> dict[str, list[Site]]:
        """Each report key's sites. A site belongs to the key one occurrence in its file
        tallies to, so the key layout stays tally's alone."""
        found: dict[str, list[Site]] = {}
        for path, source in sources:
            (key,) = cls.tally([(path, 1)])
            found.setdefault(key, []).extend(Site(path, line) for line in cls.lines(source, path))
        return found


class SourcePosition(MroDispatch):
    """The lines a counted node spans, chosen by the node's own syntax class. The parser
    positions every node a pass counts except a comprehension clause."""

    @handles(ast.stmt, ast.expr, ast.arg, ast.keyword, ast.excepthandler, ast.alias, ast.pattern)
    def positioned(self, node: ast.stmt | ast.expr | ast.arg | ast.keyword | ast.excepthandler
                   | ast.alias | ast.pattern) -> range:
        return range(node.lineno, typing.cast(int, node.end_lineno) + 1)

    def lines(self, node: ast.AST) -> range:
        return type(self).handler_for(type(node))(self, node)


class RosterPosition(SourcePosition):
    """A loop's roster is the literal it runs over, which also places a comprehension."""

    @handles(ast.For, ast.AsyncFor, ast.comprehension)
    def loop(self, node: ast.For | ast.AsyncFor | ast.comprehension) -> range:
        return self.lines(node.iter)


class NamePosition(SourcePosition):
    """A name occupies one line: an attribute's is where the attribute ends."""

    @handles(ast.Attribute)
    def attribute(self, node: ast.Attribute) -> range:
        line = typing.cast(int, node.end_lineno)
        return range(line, line + 1)

    def lines(self, node: ast.AST) -> range:
        line = super().lines(node).start
        return range(line, line + 1)


class OccurrenceMeasure(LocatedSource):
    """Shared additive counting for syntactic occurrence measures."""

    position: typing.ClassVar[SourcePosition] = SourcePosition()

    @staticmethod
    @abstractmethod
    def occurrences(node: ast.AST) -> int:
        """Return this node's contribution, excluding its descendants."""

    @staticmethod
    def sites(tree: ast.AST, filename: str) -> Iterator[ast.AST]:
        """The nodes this measure reads; every node unless a measure excludes some."""
        return ast.walk(tree)

    @classmethod
    def lines(cls, source: bytes, filename: str) -> list[int]:
        tree = cls.tree(source, filename)
        return [cls.position.lines(node).start for node in cls.sites(tree, filename)
                for _ in range(cls.occurrences(node))]

    @classmethod
    def tally(cls, counts: list[tuple[str, int]]) -> dict[str, int]:
        return {cls.__name__: sum(count for _, count in counts)}


class PerFileMeasure(CountedSource):
    """An improvement in one file cannot hide a regression in another."""

    @classmethod
    def tally(cls, counts: list[tuple[str, int]]) -> dict[str, int]:
        return {cls.key(path): count for path, count in counts}

    @classmethod
    def align(cls, base: dict[str, int], head: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
        identities = sorted(base.keys() | head.keys())
        return ({key: base.get(key, 0) for key in identities},
                {key: head.get(key, 0) for key in identities})


class PerFileOccurrenceMeasure(PerFileMeasure, OccurrenceMeasure):
    """A per-node count, kept separately for each file."""


class SyntaxPasses(InventoryMeasure[dict[type[CountedSource], int]]):
    """The front end of every CountedSource pass, as a compiler runs its passes over one tree.

    Each changed file is read and parsed once per revision and every pass counts that tree;
    the counts are stored under the file's blob, so a file an earlier run saw is not parsed
    again. The changed files of each revision are split across the pool."""

    @staticmethod
    def passes() -> tuple[type[CountedSource], ...]:
        return CountedSource.members_with(CountedSource)

    @classmethod
    def inventory(cls, repo: Path, ref: str, root: str) -> frozenset[str]:
        return python_paths(repo, ref, root)

    @classmethod
    def reading(cls, path: str, source: bytes) -> dict[type[CountedSource], int]:
        return {measure: measure.count(source, path) for measure in cls.passes()}

    @classmethod
    def measured(cls, repo: Path, ref: str, base: str, head: str,
                 readings: dict[str, dict[type[CountedSource], int]], root: str) -> dict[str, int]:
        return {key: value for measure in cls.passes() for key, value in measure.tally(
            [(path, reading[measure]) for path, reading in readings.items()]).items()}

    @classmethod
    def side(cls, side: Side, repo: Path, ref: str,
             paths: list[str]) -> tuple[Side, dict[str, dict[type[CountedSource], int]]]:
        return side, cls.read(repo, ref, paths)

    @classmethod
    def parts(cls, repo: Path, base: str, head: str, changed: set[str],
              root: str) -> tuple[Callable[[], object], ...]:
        shards = os.process_cpu_count() or 1
        return tuple(partial(cls.side, side, repo, ref, paths[shard::shards])
                     for side, ref in zip(Side, (base, head), strict=True)
                     for paths in (sorted(changed & cls.inventory(repo, ref, root)),)
                     for shard in range(min(shards, len(paths))))

    @classmethod
    def combine(cls, results: list, repo: Path, base: str, head: str, changed: set[str],
                root: str) -> tuple[dict[str, int], dict[str, int]]:
        readings: dict[Side, dict[str, dict[type[CountedSource], int]]] = {
            side: {} for side in Side}
        for side, read in results:
            readings[side].update(read)
        return PerFileMeasure.align(*(cls.measured(repo, ref, base, head,
                                                   dict(sorted(readings[side].items())), root)
                                      for side, ref in zip(Side, (base, head), strict=True)))


class MetadataKeys(ast.NodeVisitor):
    """The Python AST owns dispatch for literal and opaque metadata keys."""

    def generic_visit(self, node: ast.AST) -> int:
        return 1

    def visit_Dict(self, node: ast.Dict) -> int:
        return sum(typing.cast(int, self.visit(key)) if key else 1 for key in node.keys)

    def visit_Constant(self, node: ast.Constant) -> int:
        return int(isinstance(node.value, str))


class StringFieldMetadata(PerFileOccurrenceMeasure):
    """Dataclass instructions belong to typed annotations, not metadata dictionaries.

    Imported aliases are resolved at this Python boundary. Dynamic rebinding is
    outside this syntactic guard's proof scope, as with the existing import guards.
    """

    @staticmethod
    def sites(tree: ast.AST, filename: str) -> Iterator[ast.AST]:
        binding = DecoratorBinding.of(dataclasses.field, tree, filename)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and binding.marks(node.func):
                yield node

    @staticmethod
    def occurrences(node: ast.AST) -> int:
        descriptor: types.MemberDescriptorType = inspect.getattr_static(
            dataclasses.Field, "metadata"
        )
        metadata = descriptor.__name__
        return sum(
            typing.cast(int, MetadataKeys().visit(keyword.value))
            for keyword in typing.cast(ast.Call, node).keywords
            if keyword.arg == metadata
        )


class PlainStrIdAnnotation(PerFileOccurrenceMeasure):
    """ID-shaped fields and parameters retain their nominal kind instead of plain str."""

    @staticmethod
    def sites(tree: ast.AST, filename: str) -> Iterator[ast.AST]:
        for node in ast.walk(tree):
            match node:
                case ast.AnnAssign(
                    target=ast.Name(id=name) | ast.Attribute(attr=name), annotation=annotation
                ) | ast.arg(arg=name, annotation=annotation):
                    if not name.endswith(("_id", "nonce", "lookup", "generation")):
                        continue
                case _:
                    continue
            while True:
                match annotation:
                    case ast.Name(id=str.__name__):
                        yield node
                        break
                    case ast.BinOp(left=inner, op=ast.BitOr(), right=ast.Constant(value=None)) \
                            | ast.BinOp(left=ast.Constant(value=None), op=ast.BitOr(), right=inner):
                        annotation = inner
                    case ast.Subscript(
                        value=ast.Name(id=TypePositions.annotated)
                              | ast.Attribute(attr=TypePositions.annotated),
                        slice=ast.Tuple(elts=[inner, *_]),
                    ):
                        annotation = inner
                    case _:
                        break

    @staticmethod
    def occurrences(node: ast.AST) -> int:
        return 1


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

    @staticmethod
    def configuration(repo: Path, ref: str) -> str:
        """The revision's checker configuration blob; a revision without one has none."""
        listed = git(repo, "ls-tree", "--name-only", ref, "--", "pyproject.toml")
        return git(repo, "rev-parse", f"{ref}:pyproject.toml").decode().strip() if listed else ""

    @classmethod
    def inputs(cls, repo: Path, ref: str, head: str, root: str, selected: list[str]) -> str:
        """What the findings on the selected files depend on: the revision's whole tree, which
        a checker following imports reads, and the head's configuration."""
        tree = git(repo, "rev-parse", f"{ref}^{{tree}}").decode().strip()
        return f"{tree} {cls.configuration(repo, head)}"

    @classmethod
    def findings(cls, repo: Path, ref: str, head: str, root: str, selected: list[str],
                 side: Side) -> tuple[Side, Counter[str]]:
        return side, ContentCache.memo(
            partial(cls.checked, repo, ref, head, root, selected, side),
            cls.__name__, root, cls.inputs(repo, ref, head, root, selected), *selected)

    @classmethod
    def checked(cls, repo: Path, ref: str, head: str, root: str, selected: list[str],
                side: Side) -> Counter[str]:
        # A missing checker exits 1 like a finding run, so importing it first fails loud instead.
        importlib.import_module(cls.command(Path(), [], side)[0])
        with tempfile.TemporaryDirectory(prefix="debt-ratchet-check-") as scratch:
            tree = Path(scratch)
            cls.extract(repo, ref, tree, selected)
            configuration = tree / "pyproject.toml"
            configuration.unlink(missing_ok=True)
            if git(repo, "ls-tree", "--name-only", head, "--", "pyproject.toml"):
                configuration.write_bytes(git(repo, "show", f"{head}:pyproject.toml"))
            result = SpawnedProcess.run(
                (sys.executable, "-m", *cls.command(tree, selected, side)),
                cwd=tree, env=cls.environment(root, tree), timeout=600,
            )
        if result.returncode not in (0, 1):
            raise ValueError(f"{cls.__name__} could not check {ref}: {result.stderr}")
        # A checker reports each file under the path it was given, inside the tree or relative to
        # it; mypy also reports the modules it follows outside the tree, which are not measured.
        output = (result.stdout + result.stderr).decode()
        reported = (Path(tree, match[1]) for match in cls.finding.finditer(output))
        return Counter(path.relative_to(tree).as_posix()
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
        before, after = ({cls.key(path): found[side][path] for path in sorted(changed)}
                         for side in Side)
        return before, after


class Advisory(Measure):
    """Growth that restates no fact, such as a lint convention or layout: the ratchet warns
    on the file and never fails on it. Only measures of restated facts block a merge."""

    @classmethod
    def blocks(cls, delta: dict[str, int]) -> bool:
        return False

    @classmethod
    def grew(cls, key: str, grown: int) -> str:
        path = key.partition(":")[2]
        return f"::warning file={path}::{cls.__name__} +{grown} (warns, never blocks)"


class Unformatted(Advisory, CheckedRevision):
    """1 when Black would rewrite the file: a formatted file stays formatted."""

    finding = re.compile(r"^would reformat (.+)$", re.MULTILINE)

    @staticmethod
    def black(*arguments: str) -> list[str]:
        """Black's arguments after `python -m`. Every Black run here is already one of the
        ratchet's pool workers, so Black starts no process pool of its own."""
        return ["black", "--workers", "1", *arguments]

    @staticmethod
    def batches(selected: list[str]) -> list[list[str]]:
        # One run per pool worker: a Black start costs more than checking a file.
        shards = os.process_cpu_count() or 1
        return [selected[shard::shards] for shard in range(shards)]

    @staticmethod
    def extract(repo: Path, ref: str, scratch: Path, selected: list[str]) -> None:
        # Black reads only the file and the configuration.
        for path, source in GitMeasure.sources(repo, ref, set(selected)):
            (scratch / path).parent.mkdir(parents=True, exist_ok=True)
            (scratch / path).write_bytes(source)

    @staticmethod
    def command(tree: Path, selected: list[str], side: Side) -> list[str]:
        return Unformatted.black("--check", *selected)

    @classmethod
    def findings(cls, repo: Path, ref: str, head: str, root: str, selected: list[str],
                 side: Side) -> tuple[Side, Counter[str]]:
        """Black reads only the file and the configuration, so each file's finding is stored
        under its own blob and one Black run checks the batch's unstored files."""
        configuration = cls.configuration(repo, head)
        blobs = GitMeasure.blobs(repo, ref, set(selected))
        parts = {path: (cls.__name__, configuration, path, blobs[path]) for path in selected}
        found = {path: typing.cast(int, stored)
                 for path in selected for stored in ContentCache.stored(*parts[path])}
        fresh = [path for path in selected if path not in found]
        checked = cls.checked(repo, ref, head, root, fresh, side) if fresh else Counter()
        found |= {path: ContentCache.store(checked[path], *parts[path]) for path in fresh}
        return side, Counter(found)


class LintFindings(Advisory, CheckedRevision):
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
        cache = ContentCache.directory.with_name("debt-ratchet-mypy") / side.value
        return ["mypy", "--explicit-package-bases", "--cache-dir", str(cache), "--no-error-summary",
                *(str(tree / path) for path in selected)]

    @staticmethod
    def environment(root: str, tree: Path) -> dict[str, str]:
        """Module names start at the source directory, as they do when the package is installed."""
        return {**os.environ, "MYPYPATH": os.pathsep.join(Measure.source_paths(root, tree))}


@dataclass(frozen=True)
class ChangedSource:
    """One head module, its base revision, and the new-side lines changed by its Git patch.
    A path missing at the base is empty there, as in every per-file measure."""

    path: str
    source: bytes
    lines: frozenset[int]
    base_source: bytes

    @cached_property
    def tree(self) -> ast.Module:
        return CountedSource.tree(self.source, self.path)

    @cached_property
    def base_tree(self) -> ast.Module:
        return CountedSource.tree(self.base_source, self.path)

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
        listed = git(repo, "ls-tree", "--name-only", base, "--", path).strip()
        base_source = git(repo, "show", f"{base}:{path}") if listed else b""
        return cls(path, git(repo, "show", f"{head}:{path}"), frozenset(lines), base_source)

    @classmethod
    def departed(cls, repo: Path, base: str, head: str, path: str) -> ChangedSource:
        """A changed module present at the base, read for what left it; a module the head
        deleted is empty there."""
        listed = git(repo, "ls-tree", "--name-only", head, "--", path).strip()
        source = git(repo, "show", f"{head}:{path}") if listed else b""
        return cls(path, source, frozenset(), git(repo, "show", f"{base}:{path}"))


class ScopeNames(ast.NodeVisitor):
    """Each node's enclosing class and function names, dotted, through the AST's own dispatch."""

    def __init__(self) -> None:
        self.found: dict[ast.AST, str] = {}
        self.scope = ""

    def generic_visit(self, node: ast.AST) -> None:
        self.found[node] = self.scope
        super().generic_visit(node)

    def visit_named(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.found[node] = outer = self.scope
        self.scope = f"{outer}.{node.name}"
        super().generic_visit(node)
        self.scope = outer

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.visit_named(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_named(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.visit_named(node)


class CleanChangedLines(OccurrenceMeasure):
    """No occurrence may be written, even when another occurrence is deleted.

    An occurrence is identified by its enclosing scope and its source text, so rewriting
    the rest of a line around an unchanged occurrence writes nothing new, while a different
    occurrence elsewhere never pays for a new one. Scopes are class and function names, not
    modules, so an occurrence that leaves one changed module and arrives in another, as a
    class moved between files, is the same occurrence moved, not a new one written.
    """

    @staticmethod
    def introduced(
        repo: Path, base: str, head: str, paths: tuple[str, ...], departed_from: tuple[str, ...],
    ) -> dict[str, list[int]]:
        """Each strict measure's sites on each path's changed lines. Occurrences that left the
        changed modules in ``departed_from`` (those present at the base) pay for the same
        occurrences arriving elsewhere."""
        sources = [ChangedSource.read(repo, base, head, path) for path in paths]
        left = [ChangedSource.departed(repo, base, head, path) for path in departed_from]
        found: dict[str, list[int]] = {}
        for measure in Measure.members_with(CleanChangedLines):
            departed: Counter[tuple[str, str]] = Counter()
            for source in left:
                gone = measure.identities(source.base_tree, source.path)
                gone.subtract(measure.identities(source.tree, source.path))
                departed.update(+gone)
            for source in sources:
                found[measure.key(source.path)] = measure.changed_sites(source, departed)
        return found

    @staticmethod
    def scopes(tree: ast.AST) -> dict[ast.AST, str]:
        """Each node's enclosing class and function names, dotted; the module is empty."""
        names = ScopeNames()
        names.visit(tree)
        return names.found

    @classmethod
    def identities(cls, tree: ast.AST, filename: str) -> Counter[tuple[str, str]]:
        """Every occurrence in the module, keyed by its scope and source text."""
        scopes = cls.scopes(tree)
        found: Counter[tuple[str, str]] = Counter()
        for node in cls.sites(tree, filename):
            if count := cls.occurrences(node):
                found[scopes[node], ast.unparse(node)] += count
        return found

    @classmethod
    def changed_sites(cls, source: ChangedSource, departed: Counter[tuple[str, str]]) -> list[int]:
        """The first changed line of each occurrence written there that the base lacks and no
        changed module gave up; a moved occurrence spends the departure it came from."""
        scopes = cls.scopes(source.tree)
        unwritten = cls.identities(source.tree, source.path)
        unwritten.subtract(cls.identities(source.base_tree, source.path))
        sites = []
        for node in cls.sites(source.tree, source.path):
            if ((count := cls.occurrences(node))
                    and (touched := source.lines.intersection(cls.position.lines(node)))):
                identity = scopes[node], ast.unparse(node)
                arrived = max(0, min(count, unwritten[identity]))
                unwritten[identity] -= arrived
                moved = min(arrived, departed[identity])
                departed[identity] -= moved
                sites.extend([min(touched)] * (arrived - moved))
        return sites


class OwnInstanceWrites(ast.NodeVisitor):
    """A method body's object.__setattr__ calls on its own instance; nested scopes own theirs."""

    def __init__(self, instance: str) -> None:
        self.instance = instance
        self.found: list[ast.Call] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """A nested scope's writes are its own."""

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """A nested scope's writes are its own."""

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """A nested scope's writes are its own."""

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """A nested scope's writes are its own."""

    def visit_Call(self, node: ast.Call) -> None:
        written = [*map(ast.unparse, node.args[:1])]
        if FrozenFieldWrite.occurrences(node) and written == [self.instance]:
            self.found.append(node)
        self.generic_visit(node)


class FrozenFieldWrite(CleanChangedLines, PerFileOccurrenceMeasure):
    """object.__setattr__ writes past a frozen class; only the class's own __post_init__ may
    write its own instance, because there the instance is still being constructed."""

    construction: typing.ClassVar[str] = "__post_init__"

    @classmethod
    def sites(cls, tree: ast.AST, filename: str) -> Iterator[ast.AST]:
        constructing = {id(write) for node in ast.walk(tree) for write in cls.constructing(node)}
        return (node for node in ast.walk(tree) if id(node) not in constructing)

    @classmethod
    def constructing(cls, node: ast.AST) -> Iterator[ast.Call]:
        match node:
            case ast.ClassDef(body=members):
                for member in members:
                    match member:
                        case ast.FunctionDef(name=FrozenFieldWrite.construction,
                                             args=ast.arguments(args=[instance, *_]), body=body):
                            writes = OwnInstanceWrites(instance.arg)
                            for statement in body:
                                writes.visit(statement)
                            yield from writes.found

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

    position = NamePosition()

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
    def lines(cls, source: bytes, filename: str) -> list[int]:
        module = ChangedSource(filename, source, frozenset(), b"")
        return sorted([*super().lines(source, filename), *cls.prose_lines(module)])

    @classmethod
    def changed_sites(cls, source: ChangedSource, departed: Counter[tuple[str, str]]) -> list[int]:
        prose = cls.prose_lines(source) & source.lines
        return [*super().changed_sites(source, departed), *prose]


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
        module = AuditModule(filename, text, cls.tree(source, filename), filename)
        package = AuditPackage(AuditRepository(Path.cwd()), "", filename, (module,), ())
        return sum(1 for facts in package.functions[filename]
                   for _finding in RawShape.from_facts(facts, package))


class FunctionLocalImport(PerFileMeasure, LocatedSource):
    """Imports executed inside a function, including nested local classes."""

    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0
            self.found: list[int] = []

        def generic_visit(self, node: ast.AST) -> None:
            nested = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            self.depth += nested
            if self.depth and isinstance(node, (ast.Import, ast.ImportFrom)):
                self.found.append(node.lineno)
            super().generic_visit(node)
            self.depth -= nested

    @classmethod
    def lines(cls, source: bytes, filename: str) -> list[int]:
        collector = cls.Collector()
        collector.visit(cls.tree(source, filename))
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


class DeclaredPositions(MroDispatch):
    """Where a literal is used as a declaration's name, each position declared by its node."""

    def __init__(self) -> None:
        self.found: list[ast.expr] = []
        self.excluded: list[ast.expr] = []

    @staticmethod
    def called(node: ast.expr) -> str:
        return ast.unparse(node).rpartition(".")[2]

    @classmethod
    def literals(cls, tree: ast.AST) -> Iterator[ast.AST]:
        """The nodes inside a position, so a name spelled there names its declaration."""
        positions = cls()
        for node in ast.walk(tree):
            positions.dispatch_sync(node)
        excluded = {id(part) for expression in positions.excluded for part in ast.walk(expression)}
        return (part for expression in positions.found for part in ast.walk(expression)
                if id(part) not in excluded)


class TypePositions(DeclaredPositions):
    """The expressions where a type is expected, so a one-word class name there names it."""

    type_variable: typing.ClassVar[str] = typing.cast(type, typing.TypeVar).__name__
    annotated: typing.ClassVar[str] = typing.cast(type, Annotated).__name__

    @handles(ast.FunctionDef, ast.AsyncFunctionDef)
    def signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        arguments = node.args
        self.found.extend(filter(None, (node.returns, *(argument.annotation for argument in (
            *arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
            *filter(None, (arguments.vararg, arguments.kwarg)))))))

    @handles(ast.AnnAssign)
    def annotation(self, node: ast.AnnAssign) -> None:
        self.found.append(node.annotation)

    @handles(ast.ClassDef)
    def bases(self, node: ast.ClassDef) -> None:
        self.found.extend(node.bases)

    @handles(ast.Call)
    def type_variable_bound(self, node: ast.Call) -> None:
        if self.called(node.func) == self.type_variable:
            self.found.extend((*node.args[1:], *(keyword.value for keyword in node.keywords)))

    @handles(ast.Subscript)
    def annotated_metadata(self, node: ast.Subscript) -> None:
        """Annotated[T, ...] declares T; what follows it is metadata, not a type."""
        if self.called(node.value) == self.annotated:
            self.excluded.extend(typing.cast(ast.Tuple, node.slice).elts[1:])


class ModulePositions(DeclaredPositions):
    """Where a string selects a module: an importer's argument, a key of the module table or
    the module an interpreter runs. Anywhere else a module's name is a word, a table or a path
    that happens to share it."""

    importers: typing.ClassVar[frozenset[str]] = frozenset(
        call.__name__ for call in (importlib.import_module, __import__, importlib.util.find_spec,
                                   importlib.resources.files))
    # CPython's command-line option for running a module by name: another program's contract.
    run_module: typing.ClassVar[str] = ast.dump(ast.Constant("-m"))
    module_table: typing.ClassVar[str] = ".".join((sys.__name__, next(
        name for name, value in vars(sys).items() if value is sys.modules)))

    @handles(ast.Call)
    def imported(self, node: ast.Call) -> None:
        if self.called(node.func) in self.importers:
            self.found.extend(node.args[:1])

    @handles(ast.Call)
    def table_method(self, node: ast.Call) -> None:
        if ast.unparse(node.func).rpartition(".")[0] == self.module_table:
            self.found.extend(node.args[:1])

    @handles(ast.Subscript)
    def table_entry(self, node: ast.Subscript) -> None:
        if ast.unparse(node.value) == self.module_table:
            self.found.append(node.slice)

    @handles(ast.Compare)
    def table_membership(self, node: ast.Compare) -> None:
        if self.module_table in map(ast.unparse, node.comparators):
            self.found.append(node.left)

    @handles(ast.Tuple, ast.List)
    def interpreter_module(self, node: ast.Tuple | ast.List) -> None:
        self.found.extend(argument for option, argument in itertools.pairwise(node.elts)
                          if ast.dump(option) == self.run_module)


@dataclass(frozen=True)
class DeclaredSpellings:
    """Every spelling a revision's production Python declares as a name.

    Classes count under their own name, src modules under their dotted path, and modules and
    top-level functions under a multi-word name; a one-word module or function name is also an
    ordinary word, so a literal equal to it is not taken as naming it. A one-word class name
    (Read, Open, Claimed) is an ordinary word too, so a literal equal to it names the class
    only where a type is expected: an annotation, a return, a class base or a type variable's
    bound. A module's name names it only where a string selects a module: an importer's
    argument or a key of ``sys.modules``; elsewhere it is a word, a table or a path that shares
    the spelling. Enum members are the declared boundary spellings of another program's
    contract, so literals in an enum's body are the one place such a spelling may appear.
    """

    names: dict[str, str]
    enums: frozenset[str]
    unmistakable: dict[str, str]
    """The names a literal names wherever it appears: all but one-word classes and modules."""
    modules: dict[str, str]

    boundary_enums: typing.ClassVar[frozenset[str]] = frozenset(
        kind.__name__ for kind in (enum.Enum, enum.StrEnum, enum.IntEnum, enum.Flag, enum.IntFlag))
    named_calls: typing.ClassVar[frozenset[str]] = frozenset(
        call.__name__ for call in (getattr, setattr, hasattr, delattr, __import__,
                                   importlib.import_module))
    selecting_calls: typing.ClassVar[frozenset[str]] = frozenset(
        call.__name__ for call in (DeclaredFamily.member_named, DeclaredFamily.field_value))
    kind_spelling: typing.ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z_][\w.-]*")
    class_word: typing.ClassVar[re.Pattern[str]] = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")
    package_marker: typing.ClassVar[str] = Path(importlib.__file__).name
    entry_guard: typing.ClassVar[str] = ast.dump(ast.parse('__name__ == "__main__"', mode="eval").body)

    @staticmethod
    def multiword(name: str) -> bool:
        return name.strip("_").count("_") > 0

    @classmethod
    def one_word_class(cls, name: str) -> bool:
        return len(cls.class_word.findall(name)) == 1

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
    def import_names(cls, paths: typing.Collection[str]) -> dict[str, str]:
        """Each path's dotted import name inside a package; empty outside every package."""
        packages = {Path(path).parent for path in paths if Path(path).name == cls.package_marker}
        return {path: cls.dotted_name(Path(path).with_suffix(""), packages) for path in paths}

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
        dotted_names = cls.import_names(modules)
        for path, declared in modules.items():
            module = Path(path).with_suffix("")
            if cls.multiword(module.stem):
                names[module.stem] = f"module {path}"
            if dotted := dotted_names[path]:
                names[dotted] = f"module {path}"
            for function in declared.functions:
                names.setdefault(function, f"function {path}:{function}")
            for name, parents in declared.classes:
                names[name] = f"class {path}:{name}"
                bases.setdefault(name, set()).update(parents)
        enums = set(cls.boundary_enums)
        while grown := {name for name, parents in bases.items() if parents & enums} - enums:
            enums |= grown
        ordinary = set(filter(cls.one_word_class, (
            name for name, target in names.items() if target.startswith("class "))))
        functions = {function for declared in modules.values() for function in declared.functions}
        selected = {name: target for name, target in names.items()
                    if target.startswith("module ") and name not in functions}
        positioned = ordinary | selected.keys()
        return cls(names, frozenset(enums),
                   {name: target for name, target in names.items() if name not in positioned},
                   selected)

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
        for spellings, nodes in ((self.unmistakable, ast.walk(tree)),
                                 (self.names, TypePositions.literals(tree)),
                                 (self.modules, ModulePositions.literals(tree))):
            for node in nodes:
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and node.value in spellings):
                    record(node, NamedDeclaration, spellings[node.value])
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
        selected = readings.keys() & ChangedPaths.of(repo, base, head).touched
        return {cls.key(path): len(spellings.sites(source, path))
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

    position = RosterPosition()


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

    @staticmethod
    def carries(handler: ast.ExceptHandler) -> bool:
        """The bound error is passed into a class constructor: a typed outcome, not a swallow."""
        match handler:
            case ast.ExceptHandler(name=str(bound)):
                return any(
                    isinstance(name, ast.Name) and name.id == bound
                    for child in ast.walk(handler)
                    for argument in BroadSwallow.constructor_arguments(child)
                    for name in ast.walk(argument))
        return False

    @staticmethod
    def constructor_arguments(node: ast.AST) -> list[ast.expr]:
        """The arguments of a call to a capitalized name: a class constructor."""
        match node:
            case ast.Call(func=ast.Name(id=callee) | ast.Attribute(attr=callee),
                          args=args, keywords=keywords) if callee[:1].isupper():
                return [*args, *(keyword.value for keyword in keywords)]
        return []

    @classmethod
    def occurrences(cls, node: ast.AST) -> int:
        match node:
            case ast.With(items=items) | ast.AsyncWith(items=items):
                return sum(cls.suppresses(item.context_expr) for item in items)
            case ast.ExceptHandler(type=error):
                return int(cls.broad(error) and not cls.carries(node) and not any(
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
    # original modules own codec/schema behavior; nested package tails do not. Our own
    # modules are named by their files; Toad's live in another repository, named by path.
    mechanism_modules = frozenset((
        *(Path("src", Path(inspect.getfile(module)).relative_to(
            Path(inspect.getfile(agent_comms)).parents[1]))
          for module in (field_codec, typed_table, coordination_schema)),
        *(Path("src", "toad", module) for module in ("agent_schema.py", "db.py")),
    ))

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        if Path(filename) in cls.mechanism_modules:
            return 0
        projection = cls()
        tree = projection.visit(ast.parse(source, filename))
        return AuditFamilyFlattened.count(tree) + projection.round_trips


class MatchArms:
    """The patterns each arm of a ``match`` tests: ``case A() | B() as x:`` tests two."""

    @classmethod
    def alternatives(cls, pattern: ast.pattern) -> Iterator[ast.pattern]:
        match pattern:
            case ast.MatchOr(patterns=patterns):
                for alternative in patterns:
                    yield from cls.alternatives(alternative)
            case ast.MatchAs(pattern=ast.pattern() as inner):
                yield from cls.alternatives(inner)
            case _:
                yield pattern

    @classmethod
    def arms(cls, node: ast.Match) -> Iterator[ast.pattern]:
        for case in node.cases:
            yield from cls.alternatives(case.pattern)


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


class LiteralDispatchCases(MatchArms, DispatchCases):
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
        for arm in self.arms(node):
            match arm:
                case ast.MatchValue(value=value) if self.literal(value):
                    self.add(node.subject, repr(value.value))


class TypeTests(MatchArms, MroDispatch):
    """Every test of a value's type in Python syntax: isinstance, issubclass, ``type(x) is``,
    ``x is C``, ``x in (A, B)`` and class patterns. A reader decides what a test means."""

    instance_checks: typing.ClassVar[frozenset[str]] = frozenset(
        check.__name__ for check in (isinstance, issubclass))
    exact: typing.ClassVar[str] = type.__name__

    @abstractmethod
    def tested(self, subject: ast.expr, kind: ast.expr, at: ast.expr | ast.pattern) -> None:
        """One test of ``subject`` against ``kind``, spelled at ``at``."""

    @handles(ast.Call)
    def instance_check(self, node: ast.Call) -> None:
        match node:
            case ast.Call(func=ast.Name(id=check), args=[subject, kind],
                          keywords=[]) if check in self.instance_checks:
                self.tested(subject, kind, node)

    @handles(ast.Compare)
    def comparison(self, node: ast.Compare) -> None:
        match node:
            case ast.Compare(left=ast.Call(func=ast.Name(id=called), args=[subject]),
                             comparators=[kind]) if called == self.exact:
                self.tested(subject, kind, node)
            case ast.Compare(left=subject, ops=[ast.Is()],
                             comparators=[ast.Name() | ast.Attribute() as kind]):
                self.tested(subject, kind, node)
            case ast.Compare(left=subject, ops=[ast.In()], comparators=[ast.Tuple(elts=kinds)]) if (
                    kinds and all(isinstance(kind, (ast.Name, ast.Attribute)) for kind in kinds)):
                for kind in kinds:
                    self.tested(subject, kind, node)

    @handles(ast.Match)
    def match_cases(self, node: ast.Match) -> None:
        for arm in self.arms(node):
            match arm:
                case ast.MatchClass(cls=kind):
                    self.tested(node.subject, kind, arm)


class TypeDispatchCases(TypeTests, DispatchCases):
    def tested(self, subject: ast.expr, kind: ast.expr, at: ast.expr | ast.pattern) -> None:
        self.add(subject, ast.dump(kind))


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


class OwnTypeTests(TypeTests):
    """The classes one module tests a value against, each reported by the final name it spells."""

    def __init__(self, foreign: frozenset[str]) -> None:
        self.foreign = foreign
        self.found: list[tuple[int, str]] = []

    def tested(self, subject: ast.expr, kind: ast.expr, at: ast.expr | ast.pattern) -> None:
        for spelled in kind.elts if isinstance(kind, ast.Tuple) else (kind,):
            match spelled:
                case ast.Name(id=name) | ast.Attribute(attr=name) if (
                        ast.unparse(spelled).partition(".")[0] not in self.foreign):
                    self.found.append((at.lineno, name))

    @staticmethod
    def foreign_bindings(tree: ast.Module, ours: frozenset[str]) -> frozenset[str]:
        """Names this module binds from another program's modules: ``ast`` in ``ast.Import``."""
        bound: set[str] = set()
        for node in ast.walk(tree):
            match node:
                case ast.Import(names=names):
                    bound.update((alias.asname or alias.name).partition(".")[0] for alias in names
                                 if alias.name.partition(".")[0] not in ours)
                case ast.ImportFrom(module=module, level=0, names=names) if (
                        (module or "").partition(".")[0] not in ours):
                    bound.update(alias.asname or alias.name for alias in names)
        return frozenset(bound)

    @classmethod
    def of(cls, source: bytes, path: str, ours: frozenset[str]) -> list[tuple[int, str]]:
        tree = ast.parse(source, path)
        tests = cls(cls.foreign_bindings(tree, ours))
        for node in ast.walk(tree):
            tests.dispatch_sync(node)
        return tests.found


class OwnTypeTest(InventoryMeasure[ModuleDeclarations]):
    """Type tests against a class this repository declares, per changed file under --root.

    A test of our own class re-derives at the call site what the class's method already
    answers, so every one counts, a single arm included: the fix is a method on the base and an
    override in the member. Foreign values are dispatched once by MroDispatch at the decoder,
    which tests their foreign types, never ours. The declared classes are every production
    file's, since a tested class and its test can sit in different trees.
    """

    @classmethod
    def inventory(cls, repo: Path, ref: str, root: str) -> frozenset[str]:
        return production_paths(repo, ref)

    reading = DeclaredSpellings.declarations

    @classmethod
    def measured(cls, repo: Path, ref: str, base: str, head: str,
                 readings: dict[str, ModuleDeclarations], root: str) -> dict[str, int]:
        declared = {name for module in readings.values() for name, _ in module.classes}
        # A script outside every package, such as one in tools, is imported by its stem.
        ours = frozenset((dotted or Path(path).stem).partition(".")[0]
                         for path, dotted in DeclaredSpellings.import_names(readings).items())
        selected = {path for path in readings.keys() & ChangedPaths.of(repo, base, head).touched
                    if path.startswith(root)}
        return {cls.key(path): sum(name in declared
                                   for _, name in OwnTypeTests.of(source, path, ours))
                for path, source in GitMeasure.sources(repo, ref, selected)}

    align = PerFileMeasure.align


class BuiltinHandlerTypeSwitch(BuiltinHandlerDeclarations, PerFileMeasure):
    """Share the audit collector; per-file counts prevent moving primitive debt."""

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        if not cls.admits(filename):
            return 0
        return cls.count_module(cls.tree(source, filename))


class GodClassExcess(InventoryMeasure[dict[str, int]]):
    """Independent class lines beyond 500 in Black's layout, including newly introduced classes.
    Lines are counted after the revision's own Black configuration formats the file, so
    formatting a class never reads as growth while Unformatted asks for exactly that."""

    threshold = 500

    @classmethod
    def inventory(cls, repo: Path, ref: str, root: str) -> frozenset[str]:
        # Complete inventories distinguish a moved declaration from another
        # same-named class in an unchanged module.
        return python_paths(repo, ref, root)

    @classmethod
    def context(cls, repo: Path, ref: str) -> str:
        """Black formats each file under the revision's own configuration."""
        return CheckedRevision.configuration(repo, ref)

    @classmethod
    def read_fresh(cls, repo: Path, ref: str, paths: list[str]) -> dict[str, dict[str, int]]:
        if not paths:
            return {}
        with tempfile.TemporaryDirectory(prefix="debt-ratchet-layout-") as scratch:
            tree = Path(scratch)
            Unformatted.extract(repo, ref, tree, paths)
            if git(repo, "ls-tree", "--name-only", ref, "--", "pyproject.toml"):
                (tree / "pyproject.toml").write_bytes(git(repo, "show", f"{ref}:pyproject.toml"))
            subprocess.run(
                (sys.executable, "-m", *Unformatted.black("-q", *paths)),
                cwd=tree,
                check=True,
                timeout=600,
            )
            return {path: cls.reading(path, (tree / path).read_bytes()) for path in paths}

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
                    excess = max(0, end - start + 1 - cls.threshold)
                    values[cls.key(f"{path}::{'.'.join(scope)}")] = excess
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


class StringSubscript(OccurrenceMeasure):
    @classmethod
    def lines(cls, source: bytes, filename: str) -> list[int]:
        tree = cls.tree(source, filename)
        imports = TypingImports.of(tree)
        found = []
        for node in ast.walk(tree):
            if cls.occurrences(node):
                reference = TypingReference(imports)
                reference.dispatch_sync(node.value)
                found.extend([cls.position.lines(node).start] * reference.raw_access_count())
        return found

    @staticmethod
    def occurrences(node: ast.AST) -> int:
        return int(
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        )


class AnyReference(TypingReference):
    """A reference AnyName asks one question of: does it resolve to ``typing.Any``."""

    @property
    def is_any(self) -> bool:
        return self.value is typing.Any


class AnyInTypeExpression(MroDispatch):
    """Count ``typing.Any`` in one type expression without evaluating it.

    Only type-forming syntax is followed: a subscript, a union, a member list
    and, in an annotation, a quoted forward reference. A call or a comparison
    is a runtime value, so ``annotation is Any`` in introspection code is not a use.
    """

    def __init__(self, imports: dict[str, object], quoted: bool) -> None:
        self.imports = imports
        self.quoted = quoted
        self.references: list[ast.expr] = []

    @property
    def found(self) -> int:
        return len(self.references)

    def resolved(self, node: ast.AST) -> object:
        reference = TypingReference(self.imports)
        reference.dispatch_sync(node)
        return reference.value

    @handles(ast.Name, ast.Attribute)
    def reference(self, node: ast.expr) -> None:
        reference = AnyReference(self.imports)
        reference.dispatch_sync(node)
        if reference.is_any:
            self.references.append(node)

    @handles(ast.Subscript)
    def parameterised(self, node: ast.Subscript) -> None:
        origin = self.resolved(node.value)
        members = [node.slice]
        if origin is typing.Literal:
            members = []  # Literal members are values, not types.
        if origin is Annotated:
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
    """One written ``typing.Any`` and the declaration or reference owning it."""

    line: int
    site: str
    owner: str


class DeclaredTypeSites(MroDispatch):
    """Written Any references, with declaration-owned quoted type parsing."""

    def __init__(self, imports: dict[str, object]) -> None:
        self.imports = imports
        self.uses: list[AnyUse] = []
        self.counted: set[ast.AST] = set()

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
        self.counted.update(counted.references)
        self.uses.extend([AnyUse(expression.lineno, site, owner)] * counted.found)

    @handles(ast.Name, ast.Attribute)
    def reference(self, node: ast.Name | ast.Attribute, scope: tuple[ast.AST, ...]) -> None:
        if not isinstance(node.ctx, ast.Load) or node in self.counted:
            return
        reference = AnyReference(self.imports)
        reference.dispatch_sync(node)
        if reference.is_any:
            self.counted.add(node)
            owner = ".".join(enclosing.name for enclosing in scope)
            self.uses.append(AnyUse(node.lineno, "reference", owner))

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


class AnyName(PerFileMeasure, LocatedSource):
    """Every written Any, including quoted declarations and loaded runtime references."""

    @classmethod
    def uses(cls, source: bytes, filename: str) -> tuple[AnyUse, ...]:
        tree = cls.tree(source, filename)
        sites = DeclaredTypeSites(TypingImports.at_any_depth(tree))
        sites.read(tree)
        return tuple(sorted(sites.uses, key=lambda use: (use.line, use.site, use.owner)))

    @classmethod
    def lines(cls, source: bytes, filename: str) -> list[int]:
        return [use.line for use in cls.uses(source, filename)]


class UnannotatedParameter(PerFileOccurrenceMeasure):
    """Every function parameter declares its type; self and cls name their owner."""

    @staticmethod
    def occurrences(node: ast.AST) -> int:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return 0
        arguments = node.args
        return sum(
            argument.annotation is None and argument.arg not in ("self", "cls")
            for argument in (
                *arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                *filter(None, (arguments.vararg, arguments.kwarg)),
            )
        )


class UncarriedRecordFormat(ChangeMeasure):
    """A stored record's format changed without its version, or its version without a carry.

    Files in the old format stay on disk, so a change to a versioned record's stored form
    bumps its version, and a bumped version arrives with a new carry step that rewrites the
    old files. The stored form is what FieldCodec writes, not the annotation text: two
    representations with the same wire spelling are the same format.
    """

    probe: typing.ClassVar[str] = (
        "import importlib.util, os, sys; sys.path[:0] = sys.argv[2].split(os.pathsep); "
        "spec = importlib.util.spec_from_file_location('stored_formats', sys.argv[1]); "
        "module = sys.modules[spec.name] = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(module); "
        "module.main(sys.argv[3:])"
    )
    """This ratchet's reader, loaded by file, over only the revision's own source: a tools
    directory on the path would resolve the revision's bare imports into this one's tree."""

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
        """Each versioned record in the changed modules, measured by that revision's own code;
        the commit fixes every input, its stamp included."""
        return ContentCache.memo(partial(cls.measured_formats, repo, ref, root, changed),
                                 cls.__name__, revision(repo, ref), root, *sorted(changed))

    @classmethod
    def measured_formats(cls, repo: Path, ref: str, root: str,
                         changed: set[str]) -> dict[str, StoredForm]:
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
            tree = Path(scratch)
            for stamp in tree.rglob("package_stamp.py"):
                WheelStamp.of_source(tree / package, revision(repo, ref)).write(stamp)
            paths = dict.fromkeys((str(tree / package), *cls.source_paths(root, tree)))
            result = SpawnedProcess.run(
                (sys.executable, "-c", cls.probe,
                 str(Path(__file__).resolve().with_name("stored_formats.py")),
                 os.pathsep.join(paths), *modules), cwd=scratch,
                env=cls.environment(root, tree), timeout=300,
            )
        if result.returncode:
            raise ValueError(f"{cls.__name__} could not measure {ref}: {result.stderr.decode()}")
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


@dataclass(frozen=True)
class ChangedPaths:
    """The paths a change touches, and the moves Git pairs among them.

    Both sides of a move are touched, including moves into or out of the declared production
    source boundary, so each revision measures the path it has. A moved path's per-file counts
    then compare at its head path, so moving a file adds nothing it did not already hold."""

    touched: frozenset[str]
    moved: tuple[tuple[str, str], ...]

    @staticmethod
    @cache
    def of(repo: Path, base: str, head: str) -> ChangedPaths:
        # -z name-status: "M\0path\0", or "R096\0old\0new\0" for a move Git detected.
        fields = iter(git(repo, "diff", "--find-renames", "--name-status", "-z", base, head,
                          "--").split(b"\0")[:-1])
        touched: set[str] = set()
        moved: list[tuple[str, str]] = []
        for status in fields:
            paths = [next(fields).decode()]
            if status.startswith(b"R"):
                paths.append(next(fields).decode())
                moved.append((paths[0], paths[1]))
            touched.update(paths)
        return ChangedPaths(frozenset(touched), tuple(moved))

    def paired(self, counts: dict[str, int]) -> dict[str, int]:
        """Per-file counts ("Measure:path" or "Measure:path::member") keyed by head path."""
        moved = dict(self.moved)
        result: dict[str, int] = {}
        for identity, count in counts.items():
            measure, _, rest = identity.partition(":")
            path, separator, member = rest.partition("::")
            if path in moved:
                identity = f"{measure}:{moved[path]}{separator}{member}"
            result[identity] = result.get(identity, 0) + count
        return result


@dataclass(frozen=True)
class Comparison:
    root: str
    base_revision: str
    head_revision: str
    paths: tuple[str, ...]
    base: dict[str, int]
    head: dict[str, int]
    introduced: dict[str, list[int]]
    found: dict[str, list[Site]]

    @projected(view="report")
    def delta(self) -> dict[str, int]:
        return {name: value - self.base[name] for name, value in self.head.items()}

    @property
    def failed(self) -> bool:
        return (any(measure.blocks(self.delta) for measure in Measure.members_with(Measure))
                or any(self.introduced.values()))


def compare(repo: Path, base: str, head: str, root: str, workers: int,
            measured: tuple[type[Measure], ...] = Measure.members_with(Measure)) -> Comparison:
    source_root = Path(root)
    if source_root.is_absolute() or ".." in source_root.parts or not source_root.parts:
        raise ValueError("--root must be a repository-relative source directory")
    root = source_root.as_posix().rstrip("/") + "/"
    base, head = revision(repo, base), revision(repo, head)
    changed = ChangedPaths.of(repo, base, head)
    touched = changed.touched
    paths = set(touched & (python_paths(repo, base, root) | python_paths(repo, head, root)))
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    introduced: dict[str, list[int]] = {}
    # Every measure reads production Python paths only, so with none touched each would
    # report no change.
    if not touched & (production_paths(repo, base) | production_paths(repo, head)):
        return Comparison(root, base, head, (), before, after, introduced, {})

    # A pass of a shared runner is measured once, by that runner.
    measures = tuple(dict.fromkeys(measure.run_by() for measure in measured))
    # Forked workers start from this already-imported module instead of importing it again.
    with ForkedProcessPool(workers) as pool:
        parts = {measure: [pool.submit(part)
                           for part in measure.parts(repo, base, head, paths, root)]
                 for measure in measures}
        sites = [pool.submit(
            CleanChangedLines.introduced, repo, base, head,
            tuple(sorted(paths & python_paths(repo, head, root))),
            tuple(sorted(paths & python_paths(repo, base, root))),
        )]
        # Each measure combines as soon as its parts finish, while slower checkers still run.
        combined = {}
        owners = {future: measure for measure, futures in parts.items() for future in futures}
        for measure in measures:
            if not parts[measure]:
                combined[measure] = measure.combine([], repo, base, head, paths, root)
        for future in as_completed(owners):
            measure = owners[future]
            if measure not in combined and all(part.done() for part in parts[measure]):
                combined[measure] = measure.combine([part.result() for part in parts[measure]],
                                                    repo, base, head, paths, root)
        # A shared runner measures all its passes; the comparison holds only the asked ones.
        for measure in measures:
            for side, taken in zip((before, after), combined[measure], strict=True):
                side.update({key: value for key, value in taken.items()
                             if any(asked.owns(key) for asked in measured)})
        for path_sites in sites:
            introduced.update({identity: sorted(found)
                               for identity, found in path_sites.result().items()})
    comparison = Comparison(root, base, head, tuple(sorted(paths)), changed.paired(before),
                            changed.paired(after), introduced, {})
    # Only a grown measure is read for its sites, so a clean change pays for none.
    grown = [measure for measure in LocatedSource.members_with(LocatedSource)
             if measure in measured and measure.blocks(comparison.delta)]
    if not grown:
        return comparison
    sources = GitMeasure.sources(repo, head, paths & python_paths(repo, head, root))
    return dataclasses.replace(comparison, found={
        key: sites for measure in grown for key, sites in measure.located(sources).items()
        if comparison.delta[key] > 0})


def main() -> int:
    ContentCache.prune()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Repository-relative production source directory")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--workers", type=int, default=os.process_cpu_count() or 1,
                        help="Pool width; a caller running ratchets in parallel narrows it")
    args = parser.parse_args()
    result = compare(Path.cwd(), args.base, args.head, args.root, args.workers)
    # Flushed first: stdout is block-buffered into a pipe, and the growth lines must end the log.
    print(json.dumps(FieldCodec.project(result, "report"), indent=2), flush=True)
    # The failing step's last lines become check annotations (.github/run-step.sh merges
    # stderr), so what grew goes to stderr last; stdout stays the one JSON report.
    for measure in Measure.members_with(Measure):
        for key, grown in sorted(result.delta.items()):
            if grown > 0 and measure.owns(key):
                print(measure.grew(key, grown), file=sys.stderr)
    for name, sites in sorted(result.found.items()):
        print(f"found {name} at {' '.join(f'{site.path}:{site.line}' for site in sites)}",
              file=sys.stderr)
    for identity, lines in sorted(result.introduced.items()):
        if lines:
            print(f"introduced {identity} at lines {lines}", file=sys.stderr)
    return int(result.failed)


if __name__ == "__main__":
    raise SystemExit(main())
