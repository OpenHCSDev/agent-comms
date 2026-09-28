#!/usr/bin/env python3
"""Compare AST debt in every changed production Python path at two Git revisions."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from abc import abstractmethod
from pathlib import Path


from .declared_family import DeclaredFamily


class Measure(DeclaredFamily, affix="Measure"):
    """One syntactic measure; subclasses own their matching rule."""

    @staticmethod
    @abstractmethod
    def occurrences(node: ast.AST) -> int:
        """Return this node's contribution, excluding its descendants."""

    @classmethod
    def count(cls, source: bytes, filename: str) -> int:
        return sum(cls.occurrences(node) for node in ast.walk(ast.parse(source, filename)))


class TypeIdentity(Measure):
    @staticmethod
    def is_type_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "type"
            and len(node.args) == 1
            and not node.keywords
        )

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


class LongBooleanChain(Measure):
    @staticmethod
    def occurrences(node: ast.AST) -> int:
        return int(isinstance(node, ast.BoolOp) and len(node.values) >= 4)


class StringSubscript(Measure):
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


def compare(repo: Path, base: str, head: str, root: str) -> dict:
    source_root = Path(root)
    if source_root.is_absolute() or ".." in source_root.parts or not source_root.parts:
        raise ValueError("--root must be a repository-relative source directory")
    root = source_root.as_posix().rstrip("/") + "/"
    base, head = revision(repo, base), revision(repo, head)
    # Disable rename detection: both sides of moves enter the same union, even
    # when only one side remains a Python file or remains under the source root.
    touched = {
        path.decode()
        for path in git(repo, "diff", "--no-renames", "--name-only", "-z", base, head, "--").split(
            b"\0"
        )
    }
    paths = touched & (python_paths(repo, base, root) | python_paths(repo, head, root))
    totals = {}
    for label, ref in (("base", base), ("head", head)):
        present = paths & python_paths(repo, ref, root)
        sources = [(path, git(repo, "show", f"{ref}:{path}")) for path in sorted(present)]
        totals[label] = {
            measure.__name__: sum(measure.count(source, path) for path, source in sources)
            for measure in Measure.members_with(Measure)
        }
    delta = {name: value - totals["base"][name] for name, value in totals["head"].items()}
    return {
        "root": root,
        "base_revision": base,
        "head_revision": head,
        "paths": sorted(paths),
        **totals,
        "delta": delta,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Repository-relative production source directory")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    result = compare(Path.cwd(), args.base, args.head, args.root)
    print(json.dumps(result, indent=2))
    return int(any(value > 0 for value in result["delta"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
