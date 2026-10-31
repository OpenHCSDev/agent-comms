"""A commit that deletes a definition names it in its message, or main goes red.

On 2026-10-30 a string rewrite of commander_pings.py cut AlarmSection.tables, the
commit message never said so, an install shipped it, and every commander's board
went dark until the restore. A deletion the author meant is named in the message;
one the author did not mean is the bug. Direct pushes and admin merges skip every
pull-request gate, so this runs on each push to main, over each commit it brings.

A definition is a module-level class or function, or a function in a class body,
by its qualified name (`AlarmSection.tables`). One moved to another file of the
same commit is still defined, so only names no changed file defines afterwards
count as deleted. Naming either the qualified or the bare name is enough.

    python tools/deleted_definitions.py BEFORE AFTER
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass


def git(*arguments: str) -> str:
    return subprocess.run(("git", *arguments), check=True, capture_output=True, text=True).stdout


def definitions(source: str) -> frozenset[str]:
    """The qualified names a module defines at its top level and in its class bodies."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
            names.update(f"{node.name}.{member.name}" for member in node.body
                         if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return frozenset(names)


@dataclass(frozen=True)
class Commit:
    sha: str

    def message(self) -> str:
        return git("log", "-1", "--format=%B", self.sha)

    def changed_sources(self) -> tuple[str, ...]:
        return tuple(path for path in git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r",
                                          self.sha).splitlines()
                     if path.startswith("src/") and path.endswith(".py"))

    def defined(self, revision: str, paths: tuple[str, ...]) -> frozenset[str]:
        found: set[str] = set()
        for path in paths:
            listed = git("ls-tree", "--name-only", revision, "--", path).strip()
            if listed:
                found.update(definitions(git("show", f"{revision}:{path}")))
        return frozenset(found)

    def unnamed_deletions(self) -> tuple[str, ...]:
        paths = self.changed_sources()
        deleted = self.defined(f"{self.sha}^", paths) - self.defined(self.sha, paths)
        message = self.message()
        return tuple(sorted(name for name in deleted
                            if not any(re.search(rf"\b{re.escape(part)}\b", message)
                                       for part in (name, name.rpartition(".")[2]))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("before")
    parser.add_argument("after")
    arguments = parser.parse_args()
    commits = git("rev-list", "--reverse", "--no-merges", f"{arguments.before}..{arguments.after}").split()
    failures = [(sha, names) for sha in commits
                for names in (Commit(sha).unnamed_deletions(),) if names]
    for sha, names in failures:
        print(f"{sha[:12]} deletes {', '.join(names)} and its message never names them."
              " Name each deletion you meant; restore each one you didn't.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
