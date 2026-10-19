"""The steps a retained bus is carried through when its durable format changes.

A step is one module beside this one, ``carry_<what>.py``, holding one ``Carry``
subclass, run as ``python -m cutover.carry_<what> ROOT [--apply]`` with ``src``
and ``tools`` on the path. Three rules make a step safe to run on any bus at any later time:

- It is idempotent: data it already carried reports no change.
- It names the step it runs ``after`` (``None`` for the first), so the order is
  declared once, by the step that depends on it.
- Its conversion is never edited once it is on main; a different change is a
  new step. Its predecessor may change when a finished step is retired.

A finished step is deleted and its successor names the preceding kept step.
Only predecessors still explicitly named by a kept step are recovered from Git;
unreferenced deleted steps cannot run again.

Each bus records the steps applied to it in ``carries.json``. Only this family
reads or writes that file; nothing in ``src/`` depends on it.
"""
from __future__ import annotations

import argparse
import ast
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
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

    def applied(self) -> tuple[str, ...]:
        """Empty when this bus predates the ledger: it runs every step, and each
        leaves already carried data unchanged."""
        if not self.path.exists():
            return ()
        return tuple(json.loads(self.path.read_text())["applied"])

    def record(self, name: str) -> None:
        """Called with the bus's wire lock held."""
        applied = self.applied()
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
    def module(cls) -> Path:
        return Path(inspect.getfile(cls)).resolve()

    @classmethod
    def name(cls) -> str:
        return cls.module().stem

    @classmethod
    def chain(cls) -> CarryChain:
        """The chain declared by the revision this step's module was checked out from."""
        checkout = git(cls.module().parent, "rev-parse", "--show-toplevel").decode().strip()
        return CarryChain.at(Path(checkout), "HEAD")

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
            if args.apply and not args.before_stop:
                applied = CarryLedger(args.root).applied()
                try:
                    cls.chain().require_next(cls.name(), applied)
                except ValueError as error:
                    raise SystemExit(f"{cls.name()}: {error}") from None
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
    def carried(
        cls, root: Path, changed: int, detail: Mapping[str, int], *, apply: bool
    ) -> Carried:
        rewritten = tuple(path.name for path in root.glob(f"{TodoStore.path_in(root).name}*"))
        return Carried(changed, detail, rewritten if changed and apply else ())


@dataclass(frozen=True)
class Step:
    """One step as a revision's tree declares it."""

    name: str
    after: str | None
    revision: str
    columns: tuple[str, ...] = ()
    """The todo column this step adds, as its ``column`` declares it."""

    @staticmethod
    def declared_columns(source: bytes) -> tuple[str, ...]:
        """The ``column`` a step module's class declares, read without importing it."""
        return tuple(ast.literal_eval(statement.value)
                     for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
                     for statement in node.body if isinstance(statement, ast.Assign)
                     and [target.id for target in statement.targets
                          if isinstance(target, ast.Name)] == ["column"])

    @classmethod
    def declared(cls, name: str, source: bytes, revision: str) -> Step | None:
        """The step a module declares, read without importing it; None if it holds no Carry."""
        for node in ast.parse(source).body:
            if isinstance(node, ast.ClassDef) and any(
                parent_name(base) in carry_parents() for base in node.bases
            ):
                for statement in node.body:
                    if (isinstance(statement, ast.Assign)
                            and [target.id for target in statement.targets
                                 if isinstance(target, ast.Name)] == ["after"]):
                        return cls(name, ast.literal_eval(statement.value), revision,
                                   cls.declared_columns(source))
                raise ValueError(f"{name} declares no step it runs after")
        return None


def parent_name(base: ast.expr) -> str | None:
    """The class a step names as its parent, bare or through its module."""
    match base:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
    return None


def carry_parents() -> frozenset[str]:
    """Carry and every parent this module gives a step to inherit."""
    return frozenset(name for name, value in globals().items()
                     if isinstance(value, type) and issubclass(value, Carry))


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


@dataclass(frozen=True)
class CarryChain:
    """Every step a revision's history declares, in the order they run, and the
    steps that history retired by deleting them."""

    steps: tuple[Step, ...]
    retired: frozenset[str] = frozenset()
    retired_columns: frozenset[str] = frozenset()
    """Todo columns added by retired steps; a bus that never ran them still lacks them."""
    added: Mapping[str, int] = field(default_factory=dict)
    """Each step's first main-line addition, numbered oldest first."""

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
        # Only a declared predecessor can keep a historical step reachable.
        # Deleting a finished step and reconnecting its successor retires it.
        waiting = list(known.values())
        while waiting:
            step = waiting.pop()
            if step.after is not None and step.after not in known:
                predecessor = cls.deleted(repo, revision, step.after)
                if predecessor is None:
                    raise ValueError(f"Carry step {step.after} is in no revision of {revision}")
                known[predecessor.name] = predecessor
                waiting.append(predecessor)
        retired = cls.retired_at(repo, revision, known)
        retired_columns = frozenset(
            column for name in retired
            for column in Step.declared_columns(cls.source(repo, revision, name)[1]))
        if not known:
            return cls((), retired, retired_columns)
        added = cls.added_at(repo, revision)
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
        return cls(tuple(reversed(steps)), retired, retired_columns, added)

    @staticmethod
    def added_at(repo: Path, revision: str) -> dict[str, int]:
        """Number each step file's first main-line addition, oldest first."""
        log = git(repo, "log", "-m", "--first-parent", "--no-renames", "--diff-filter=A",
                  "--reverse", "--name-only", "--format=%x00", revision, "--", str(STEPS))
        added: dict[str, int] = {}
        for index, commit in enumerate(log.decode().split("\0")):
            for path in commit.split():
                added.setdefault(Path(path).stem, index)
        return added

    @staticmethod
    def retired_at(repo: Path, revision: str, kept: Mapping[str, Step]) -> frozenset[str]:
        """Step files the main line deleted without keeping them reachable.

        A finished step is retired by deleting its file and reconnecting its
        successor; a bus that already ran it keeps the name in its ledger."""
        deleted = git(repo, "log", "-m", "--first-parent", "--no-renames", "--diff-filter=D",
                      "--name-only", "--format=", revision, "--", str(STEPS)).decode().split()
        return frozenset(Path(path).stem for path in deleted
                         if Path(path).parent == STEPS and Path(path).name.startswith("carry_")
                         and path.endswith(".py")) - set(kept)

    @staticmethod
    def deleted(repo: Path, revision: str, name: str) -> Step | None:
        """A step no longer in the tree, at the main-line revision that last wrote it: the
        source it was written against, not the later source beside its deletion. None if
        that revision held a plain script rather than a step."""
        written, source = CarryChain.source(repo, revision, name)
        return Step.declared(name, source, written)

    @staticmethod
    def source(repo: Path, revision: str, name: str) -> tuple[str, bytes]:
        """The main-line revision that last wrote a step file, and that file."""
        path = f"{STEPS}/{name}.py"
        written = git(repo, "log", "-m", "--first-parent", "--no-renames", "-1", "--format=%H",
                      "--diff-filter=AM", revision, "--", path).decode().strip()
        return written, git(repo, "show", f"{written}:{path}")

    def lacking(self, name: str) -> frozenset[str]:
        """Todo columns a bus may still lack when step ``name`` runs: its own, those the
        kept steps after it add, and those retired steps added."""
        names = [step.name for step in self.steps]
        return frozenset(column for step in self.steps[names.index(name):]
                         for column in step.columns) | self.retired_columns

    def require_next(self, name: str, applied: tuple[str, ...]) -> None:
        """A step applies to a bus that already records it or whose next pending step it is.
        Otherwise the bus first runs the pending steps through this one, as
        ``tools/upgrade_bus.py`` does; a ledger that pending() refuses stays refused."""
        if name in applied:
            return
        pending = [step.name for step in self.pending(applied)]
        if pending[:1] != [name]:
            ahead = pending[: pending.index(name) + 1] if name in pending else pending
            raise ValueError(f"the bus must first run {ahead}; run tools/upgrade_bus.py")

    def pending(self, applied: tuple[str, ...]) -> tuple[Step, ...]:
        """The steps a bus still needs. A recorded step must be kept or retired. A bus may
        lack an earlier kept step only when the main line added it after a later step
        the bus records; otherwise it was carried out of order, and is refused."""
        names = [step.name for step in self.steps]
        unknown = set(applied) - set(names) - self.retired
        if unknown:
            raise ValueError(f"Bus records steps this history does not declare: {sorted(unknown)}")
        done = [name in applied for name in names]
        for index, name in enumerate(names):
            if not done[index] and any(
                later in applied and self.added[later] >= self.added[name]
                for later in names[index + 1:]
            ):
                raise ValueError("Bus ledger skips a step that a later recorded step runs after")
        return tuple(step for step, applied_step in zip(self.steps, done) if not applied_step)


class TodoColumnCarry(Carry):
    """Todo rows gain one declared record column, empty; every other column stays as it was."""

    column: ClassVar[str]

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = TodoStore.path_in(root)
        if not path.exists():
            return Carried(0)
        with closing(CoordinationStore.writer(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = cls._carry(db, apply=apply)
            except BaseException:
                db.rollback()
                raise
        rewritten = tuple(item.name for item in root.glob(f"{path.name}*"))
        return Carried(changed, {}, rewritten if changed and apply else ())

    @classmethod
    def _carry(cls, db: sqlite3.Connection, *, apply: bool) -> int:
        table, fields = Todo.declared_name, Todo._fields()
        actual = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
        expected = tuple(item.wire_name for item in fields)
        if actual == expected:
            db.rollback()
            return 0
        # The chain says which declared columns this bus may still lack here; the
        # rebuild fills each one empty, as the step that declares it does.
        added = tuple(item for item in fields if item.wire_name not in actual)
        lacking = cls.chain().lacking(cls.name())
        if (actual != tuple(item.wire_name for item in fields if item not in added)
                or cls.column not in {item.name for item in added}
                or not {item.name for item in added} <= lacking):
            raise ValueError(f"Unexpected todo columns: {actual!r}")
        selected = ", ".join(f'? AS "{item.wire_name}"' if item in added else f'"{item.wire_name}"'
                             for item in fields)
        empty = tuple(item.encode(()) for item in added)
        # Rows decode only once every step before the last has carried them; this one
        # adds columns, so it counts rows without reading them as today's Todo.
        count = int(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
        if not apply:
            db.rollback()
            return count
        saved = f"{table}_before_{cls.column}"
        indexes = tuple(db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,)))
        for name, in indexes:
            db.execute(f'DROP INDEX "{name}"')
        db.execute(f'ALTER TABLE "{table}" RENAME TO "{saved}"')
        Todo.create(db)
        columns = Todo._column_list(expected)
        db.execute(f'INSERT INTO "{table}" ({columns}) SELECT {selected} FROM "{saved}"', empty)
        db.execute(f'DROP TABLE "{saved}"')
        db.commit()
        return count
