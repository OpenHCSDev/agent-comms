#!/usr/bin/env python3
"""Compare AST debt in every changed production Python path at two Git revisions."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path


from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, projected


class Measure(DeclaredFamily, affix="Measure"):
    @classmethod
    @abstractmethod
    def compare(cls, repo: Path, base: str, head: str, changed: set[str], root: str):
        """Return independent before/after measurements for this declaration."""

    @staticmethod
    def difference(before: int | None, after: int) -> int | None:
        return None if before is None else after - before


class GitMeasure(Measure):
    """Shared Git collection and alignment for declaration-owned snapshots."""

    @classmethod
    def scope(cls, changed: set[str], present: set[str]) -> set[str]:
        return changed & present

    @classmethod
    @abstractmethod
    def snapshot(cls, sources: list[tuple[str, bytes]]) -> dict[str, int]:
        """Measure the selected source declarations."""

    @classmethod
    def align(cls, base: dict[str, int], head: dict[str, int]) -> tuple[dict[str, int | None], dict[str, int]]:
        return base, head

    @classmethod
    def compare(cls, repo: Path, base: str, head: str, changed: set[str], root: str):
        def snapshot(ref: str) -> dict[str, int]:
            selected = cls.scope(changed, python_paths(repo, ref, root))
            return cls.snapshot([(path, git(repo, "show", f"{ref}:{path}")) for path in sorted(selected)])
        return cls.align(snapshot(base), snapshot(head))


class OccurrenceMeasure(GitMeasure):
    """Shared additive counting for syntactic occurrence measures."""

    @staticmethod
    @abstractmethod
    def occurrences(node: ast.AST) -> int:
        """Return this node's contribution, excluding its descendants."""

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        return sum(cls.occurrences(node) for node in ast.walk(ast.parse(source, filename)))

    @classmethod
    def snapshot(cls, sources: list[tuple[str, bytes]]) -> dict[str, int]:
        return {cls.__name__: sum(cls.count(source, path) for path, source in sources)}


class ClassSize(GitMeasure):
    """Independent lexical line spans for every class, never summed together."""

    @classmethod
    def scope(cls, changed: set[str], present: set[str]) -> set[str]:
        # Complete inventories distinguish a moved declaration from another
        # same-named class in an unchanged module.
        return present

    @classmethod
    def snapshot(cls, sources: list[tuple[str, bytes]]) -> dict[str, int]:
        values = {}
        def visit(node: ast.AST, path: str, scope: tuple[str, ...]) -> None:
            match node:
                case ast.ClassDef(name=name, lineno=start, end_lineno=end, decorator_list=decorators):
                    scope = (*scope, name)
                    start = min([start, *(decorator.lineno for decorator in decorators)])
                    values[f"{cls.__name__}:{path}::{'.'.join(scope)}"] = end - start + 1
                case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                    scope = (*scope, name)
            for child in ast.iter_child_nodes(node):
                visit(child, path, scope)
        for path, source in sources:
            visit(ast.parse(source, path), path, ())
        return values

    @classmethod
    def align(cls, base: dict[str, int], head: dict[str, int]) -> tuple[dict[str, int | None], dict[str, int]]:
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
                # A new owner has no main baseline yet; its measured size
                # becomes that baseline once merged. Do not fabricate zero.
                matched[identity] = None
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
    @staticmethod
    def occurrences(node: ast.AST) -> int:
        return int(isinstance(node, ast.BoolOp) and len(node.values) >= 4)


class StringSubscript(OccurrenceMeasure):
    @staticmethod
    def occurrences(node: ast.AST) -> int:
        return int(
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        )


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], timeout=20)


def revision(repo: Path, ref: str) -> str:
    return (
        git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    )


def python_paths(repo: Path, ref: str, root: str) -> set[str]:
    return {
        path.decode()
        for path in git(repo, "ls-tree", "-rz", "--name-only", ref, "--", root).split(b"\0")
        if path.endswith(b".py")
    }


@dataclass(frozen=True)
class Comparison:
    root: str
    base_revision: str
    head_revision: str
    paths: tuple[str, ...]
    base: dict[str, int | None]
    head: dict[str, int]

    @projected(view="report")
    def delta(self) -> dict[str, int | None]:
        return {name: Measure.difference(self.base[name], value) for name, value in self.head.items()}

    @property
    def increased(self) -> bool:
        return any(value is not None and value > 0 for value in self.delta.values())


def compare(repo: Path, base: str, head: str, root: str) -> Comparison:
    source_root = Path(root)
    if source_root.is_absolute() or ".." in source_root.parts or not source_root.parts:
        raise ValueError("--root must be a repository-relative source directory")
    root = source_root.as_posix().rstrip("/") + "/"
    base, head = revision(repo, base), revision(repo, head)
    # Disable rename detection: both sides of a move enter the union, including
    # moves into/out of the declared production source boundary.
    touched = {
        path.decode()
        for path in git(repo, "diff", "--no-renames", "--name-only", "-z", base, head, "--").split(b"\0")
    }
    paths = touched & (python_paths(repo, base, root) | python_paths(repo, head, root))

    before, after = {}, {}
    for measure in Measure.members_with(Measure):
        measured_base, measured_head = measure.compare(repo, base, head, paths, root)
        before.update(measured_base)
        after.update(measured_head)
    return Comparison(root, base, head, tuple(sorted(paths)), before, after)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Repository-relative production source directory")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    result = compare(Path.cwd(), args.base, args.head, args.root)
    print(json.dumps(FieldCodec.project(result, "report"), indent=2))
    return int(result.increased)


if __name__ == "__main__":
    raise SystemExit(main())
