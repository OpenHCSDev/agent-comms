"""The steps a retained bus is carried through when its durable format changes.

A step is one module beside this one, ``carry_<what>.py``, holding one ``Carry``
subclass, run as ``python -m cutover.carry_<what> ROOT [--apply]`` with ``src``
and ``tools`` on the path. Three rules make a step safe to run on any bus at any later time:

- It is idempotent: data it already carried reports no change.
- It names the step it runs ``after`` (``None`` for the first), so the order is
  declared once, by the step that depends on it.
- It is never edited once it is on main; a different change is a new step.
  The debt ratchet's ``EditedCarry`` measure refuses an edit, a rename and the
  reuse of a deleted step's name.

A step that can no longer run against the current source is deleted. Its code
stays in Git: a bus whose ledger lacks it runs it from the last revision that
held it (``tools/upgrade_bus.py``). So deleting a step never strands a bus.

Each bus records the steps applied to it in ``carries.json``. Only this family
reads or writes that file; nothing in ``src/`` depends on it.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from agent_comms.coordination_database import CoordinationStore
from agent_comms.todos import Todo, TodoStore

STEPS = Path("tools/cutover")


@dataclass(frozen=True)
class Carried:
    """What one run of a step changed, or would change on a dry run."""

    changed: int
    detail: Mapping[str, int | bool] = field(default_factory=dict)
    rewritten: tuple[str, ...] = ()
    """Files under the bus root this run wrote; every other file stays byte-identical."""


@dataclass(frozen=True)
class CarryLedger:
    """The steps applied to one bus, oldest first."""

    root: Path

    name: ClassVar[str] = "carries.json"

    @property
    def path(self) -> Path:
        return self.root / self.name

    def applied(self) -> tuple[str, ...] | None:
        """None when this bus predates the ledger."""
        if not self.path.exists():
            return None
        return tuple(json.loads(self.path.read_text())["applied"])

    def record(self, name: str) -> None:
        """Called with the bus's wire lock held."""
        applied = self.applied() or ()
        if name in applied:
            return
        payload = json.dumps({"applied": [*applied, name]}, indent=2) + "\n"
        descriptor, stage = tempfile.mkstemp(dir=self.root, prefix=".carries.", suffix=".json")
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, payload.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(stage, self.path)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class Carry(ABC):
    after: ClassVar[str | None]

    @classmethod
    def name(cls) -> str:
        return Path(sys.modules[cls.__module__].__file__).stem

    @classmethod
    @abstractmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        """Carry ``root`` with its wire lock held; on a dry run, write nothing."""

    @classmethod
    def before_stop(cls, root: Path, *, apply: bool) -> Carried:
        """What the running installation needs before its own stop command can read the bus.
        Run with that installation's ``src``; it records nothing in the ledger."""
        return Carried(0)

    @classmethod
    def main(cls) -> None:
        from agent_comms.store_files import _store_lock

        parser = argparse.ArgumentParser(description=sys.modules[cls.__module__].__doc__)
        parser.add_argument("root", type=lambda value: Path(value).expanduser().resolve())
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--before-stop", action="store_true")
        args = parser.parse_args()
        with _store_lock(args.root / "wire"):
            if args.before_stop:
                result = cls.before_stop(args.root, apply=args.apply)
            else:
                result = cls.carry(args.root, apply=args.apply)
                if args.apply:
                    CarryLedger(args.root).record(cls.name())
                    result = replace(result, rewritten=(*result.rewritten, CarryLedger.name))
        print(json.dumps({"carry": cls.name(), "changed": result.changed, "applied": args.apply,
                          "rewritten": result.rewritten, **result.detail}))



class TodoRows(Carry):
    """A carry over the saved todo rows: one write transaction on the todo store that
    re-reads every row as declared, then commits on apply and rolls back otherwise."""

    @classmethod
    @contextmanager
    def todo_rows(cls, root: Path, *, apply: bool) -> Iterator[sqlite3.Connection | None]:
        """None when this bus has no todo store."""
        if not TodoStore.path_in(root).exists():
            yield None
            return
        with closing(CoordinationStore.writer(TodoStore.path_in(root))) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                for _row in Todo.stream(db):
                    pass
                if apply:
                    db.commit()
                else:
                    db.rollback()
            except BaseException:
                db.rollback()
                raise

    @classmethod
    def carried(cls, root: Path, changed: int, detail: Mapping[str, int], *, apply: bool) -> Carried:
        rewritten = tuple(path.name for path in root.glob(f"{TodoStore.path_in(root).name}*"))
        return Carried(changed, detail, rewritten if changed and apply else ())


@dataclass(frozen=True)
class Step:
    """One step as a revision's tree declares it."""

    name: str
    after: str | None
    revision: str

    @classmethod
    def declared(cls, name: str, source: bytes, revision: str) -> Step | None:
        """The step a module declares, read without importing it; None if it holds no Carry."""
        for node in ast.parse(source).body:
            if isinstance(node, ast.ClassDef) and any(
                isinstance(base, ast.Name) and base.id == "Carry" for base in node.bases
            ):
                for statement in node.body:
                    if (isinstance(statement, ast.Assign)
                            and [target.id for target in statement.targets
                                 if isinstance(target, ast.Name)] == ["after"]):
                        return cls(name, ast.literal_eval(statement.value), revision)
                raise ValueError(f"{name} declares no step it runs after")
        return None


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


@dataclass(frozen=True)
class CarryChain:
    """Every step a revision's history declares, in the order they run."""

    steps: tuple[Step, ...]

    @classmethod
    def at(cls, repo: Path, revision: str) -> CarryChain:
        """Steps in the tree, and every step its history added and later deleted."""
        known = {}
        tree = git(repo, "ls-tree", "--name-only", f"{revision}:{STEPS}").decode().split()
        for path in tree:
            if path.startswith("carry_") and path.endswith(".py"):
                name = path.removesuffix(".py")
                step = Step.declared(name, git(repo, "show", f"{revision}:{STEPS}/{path}"), revision)
                if step is not None:
                    known[name] = step
        for path in git(repo, "log", "--no-renames", "--format=", "--name-only", "--diff-filter=A", revision,
                        "--", str(STEPS)).decode().split():
            name = Path(path).stem
            if name.startswith("carry_") and path.endswith(".py") and Path(path).name not in tree:
                step = cls.deleted(repo, revision, name)
                if step is not None:
                    known[name] = step
        if not known:
            return cls(())
        followed = {step.after for step in known.values()}
        heads = [step for step in known.values() if step.name not in followed]
        if len(heads) != 1:
            raise ValueError(f"Carry steps must form one chain; heads: {sorted(s.name for s in heads)}")
        steps = []
        step = heads[0]
        while True:
            steps.append(step)
            if step.after is None:
                break
            if step.after not in known:
                raise ValueError(f"Carry step {step.after} is in no revision of {revision}")
            step = known[step.after]
            if step in steps:
                raise ValueError(f"Carry chain loops at {step.name}")
        if len(set(known) - {step.name for step in steps}):
            raise ValueError("Carry steps must form one chain")
        return cls(tuple(reversed(steps)))

    @staticmethod
    def deleted(repo: Path, revision: str, name: str) -> Step | None:
        """A step no longer in the tree, at the last revision that held it; None if
        that revision held a plain script rather than a step."""
        path = f"{STEPS}/{name}.py"
        deletion = git(repo, "log", "--no-renames", "-1", "--format=%H", "--diff-filter=D", revision, "--", path).decode().strip()
        holder = git(repo, "rev-parse", f"{deletion}^").decode().strip()
        return Step.declared(name, git(repo, "show", f"{holder}:{path}"), holder)

    def pending(self, applied: tuple[str, ...]) -> tuple[Step, ...]:
        """The steps a bus still needs. A bus recording a later step but not an
        earlier one was carried out of order, and is refused."""
        names = [step.name for step in self.steps]
        unknown = set(applied) - set(names)
        if unknown:
            raise ValueError(f"Bus records steps this history does not declare: {sorted(unknown)}")
        done = [name in applied for name in names]
        if done != sorted(done, reverse=True):
            raise ValueError("Bus ledger skips a step that a later recorded step runs after")
        return tuple(step for step, applied_step in zip(self.steps, done) if not applied_step)
