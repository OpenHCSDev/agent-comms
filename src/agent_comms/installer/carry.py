"""The steps a retained bus is carried through when its durable format changes.

A step is one module in ``tools/cutover``, ``carry_<what>.py``, holding one ``Carry``
subclass. Only ``agent-comms install`` runs it, through ``step_worker`` with the ``src``
and ``tools`` of the revision its family names, so no step runs outside its chain.
Three rules make a step safe to run on any bus at any later time:

- It is idempotent: data it already carried reports no change.
- It runs in the order the main line added it: the first-parent commit that adds its
  file places it, so merge order is chain order and no step restates its neighbour.
  The one exception is typed on the step families: a family ``precedes`` the families
  whose stores it reshapes, since every step reads stores as today's source declares them.
- Its conversion is never edited once it is on main; a different change is a
  new step.

A finished step is retired by deleting its file; it never runs again, and a bus
that already ran it keeps the name in its ledger.

Each bus records the steps applied to it in ``carries.json``. Only this family
reads or writes that file; nothing in ``src/`` depends on it.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import sqlite3
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar, cast

from agent_comms.coordination_database import CoordinationStore
from agent_comms.todos import Todo, TodoStore
from agent_comms.typed_table import TypedStore

STEPS = Path("tools/cutover")
FRAMEWORK = Path("src/agent_comms/installer/carry.py")
"""This module, whose ``Carry`` headers the chain compiles at each revision."""


@dataclass(frozen=True)
class Carried:
    """What one run of a step changed, or would change on a dry run."""

    changed: int
    detail: dict[str, int | bool] = field(default_factory=dict)
    rewritten: tuple[str, ...] = ()
    """Files under the bus root this run wrote; every other file stays byte-identical."""


    def report(self, name: str, applied: bool) -> dict[str, object]:
        return {"carry": name, "changed": self.changed, "applied": applied,
                "rewritten": self.rewritten, **self.detail}


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

    def apply(self, chain: CarryChain, name: str, operation: Callable[[], Carried]) -> Carried:
        """The caller holds the wire lock across ordering, conversion and recording."""
        applied = self.applied()
        chain.require_next(name, applied)
        if name in applied:
            return Carried(0)
        result = operation()
        self.record(name)
        return replace(result, rewritten=(*result.rewritten, self.name))

    def record(self, name: str) -> None:
        """Called with the bus's wire lock held."""
        applied = self.applied()
        if name not in applied:
            self.write((*applied, name))

    def start(self, chain: CarryChain) -> None:
        """A bus this chain's revision created is already in its format: every step it
        declares counts as applied, so none runs on it."""
        self.write(tuple(step.name for step in chain.steps))

    def write(self, applied: tuple[str, ...]) -> None:
        payload = json.dumps({"applied": list(applied)}, indent=2) + "\n"
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
    @classmethod
    def precedes(cls, later: type[Carry]) -> bool:
        """Whether a step of this family runs before a step of the ``later`` family that
        the main line added earlier. A step runs with today's source, so a step that
        reshapes a store runs before the steps that read that store as declared."""
        return False

    @classmethod
    def source_revision(cls, step: Step) -> str:
        """The revision whose src runs ``step``: the main-line revision that last wrote it."""
        return step.written

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
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        raise RuntimeError(f'{cls.name()} needs the full agent-comms install operation')

    @classmethod
    @abstractmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        """Carry ``root`` with its wire lock held; on a dry run, write nothing."""

    @classmethod
    def before_stop(cls, root: Path, *, apply: bool) -> Carried:
        """What the running installation needs before its own stop command can read the bus.
        Run with that installation's ``src``; it records nothing in the ledger."""
        return Carried(0)


class StoreCarry(Carry):
    """A declared SQLite store supplies the backup boundary for its conversion."""
    store: ClassVar[type[TypedStore]]

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return (cls.store.path_in(root),)


    @classmethod
    def has_store(cls, root: Path) -> bool:
        return cls.store.path_in(root).exists()

    @classmethod
    @contextmanager
    def store_transaction(cls, root: Path, *, apply: bool) -> Iterator[sqlite3.Connection | None]:
        """None when this bus has no store; dry runs and errors always roll back."""
        if not cls.has_store(root):
            yield None
            return
        with closing(CoordinationStore.writer(cls.store.path_in(root))) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
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
        rewritten = tuple(path.name for path in root.glob(f"{cls.store.path_in(root).name}*"))
        return Carried(changed, dict(detail), rewritten if changed and apply else ())


class TodoRows(StoreCarry):
    """Re-read the declared todo rows before the shared store transaction commits."""

    store = TodoStore

    @classmethod
    def source_revision(cls, step: Step) -> str:
        """Every column step runs before a row reader, so rows decode only as the chain's
        own Todo; an older tree's Todo lacks the columns added since it was written."""
        return step.revision

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        if not cls.has_store(root):
            return cls.carried(root, 0, {}, apply=apply)
        with cls.todo_rows(root, apply=apply) as db:
            return cls.carry_rows(root, cast(sqlite3.Connection, db), apply=apply)

    @classmethod
    @abstractmethod
    def carry_rows(cls, root: Path, db: sqlite3.Connection, *, apply: bool) -> Carried:
        """Convert an existing store; the step owns its changed count and detail."""

    @classmethod
    @contextmanager
    def todo_rows(cls, root: Path, *, apply: bool) -> Iterator[sqlite3.Connection | None]:
        with cls.store_transaction(root, apply=apply) as db:
            yield db
            if db is not None:
                for _row in Todo.stream(db):
                    pass


@dataclass(frozen=True)
class Step:
    """One step as a revision's tree declares it."""

    name: str
    revision: str
    """The revision whose chain holds this step."""
    written: str
    """The main-line revision that last wrote the step; it runs with that tree's src."""
    declaration: str
    family: type[Carry]
    """The family the step's class inherits from, as its module names it."""
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

    @staticmethod
    def holds_carry(source: bytes, parents: Mapping[str, type[Carry]]) -> bool:
        """Whether a module declares a Carry subclass, read without importing it."""
        return any(isinstance(node, ast.ClassDef)
                   and any(parent_name(base) in parents for base in node.bases)
                   for node in ast.parse(source).body)

    @classmethod
    def declared(
        cls, name: str, source: bytes, revision: str, written: str,
        parents: Mapping[str, type[Carry]],
    ) -> Step:
        """The step a module declares, read without importing it. A ``carry_*`` module
        holding no Carry is refused: a conversion outside the chain never runs."""
        for node in ast.parse(source).body:
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    if (parent := parent_name(base)) in parents:
                        return cls(name, revision, written, node.name, parents[parent],
                                   cls.declared_columns(source))
        raise ValueError(f"{name} holds no Carry step; every carry_* module is a step of the chain")


def parent_name(base: ast.expr) -> str:
    """Decode a static parent reference; an unsupported expression is not absence."""
    match base:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
        case ast.Subscript(value=parent):
            return parent_name(parent)
    raise ValueError(f"Unsupported carry parent expression: {ast.dump(base)}")


def carry_parents(source: bytes) -> dict[str, type[Carry]]:
    """The target's parent declarations and ordering behavior, without its Core imports.

    Compile only Carry headers and their ordering method. Python owns inheritance
    and C3 resolution; no second interpreter or restated precedence policy does.
    """
    parents: dict[str, type[Carry]] = {}

    def declared(parent: type[Carry]) -> type[Carry]:
        parents[parent.__name__] = parent
        return parent

    names = {Carry.__name__}
    declarations: list[ast.stmt] = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    ]
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != Carry.__name__ and not any(
            parent_name(base) in names for base in node.bases
        ):
            continue
        methods: list[ast.stmt] = [
            method for method in node.body
            if isinstance(method, ast.FunctionDef)
            and method.name in {Carry.precedes.__name__, Carry.source_revision.__name__}
        ]
        if node.name == Carry.__name__:
            if not methods:
                raise ValueError("Target Carry declares no ordering behavior")
            node.bases = [ast.Name(id=Carry.__name__, ctx=ast.Load())]
        node.body = methods or [ast.Pass()]
        node.decorator_list = [ast.Name(id=declared.__name__, ctx=ast.Load())]
        declarations.append(node)
        names.add(node.name)
    tree = ast.fix_missing_locations(ast.Module(body=declarations, type_ignores=[]))
    exec(compile(tree, str(FRAMEWORK), "exec"),
         {Carry.__name__: Carry, declared.__name__: declared})
    if Carry.__name__ not in parents:
        raise ValueError("Target tree declares no Carry family")
    return parents


class UnorderedSteps(ValueError):
    """One commit adds several steps, so the main line gives them no order."""

    def __init__(self, commit: str, names: list[str]) -> None:
        super().__init__(f"{commit[:12]} adds carry steps {sorted(names)} in one commit; "
                         "add each in its own commit so the main line orders them")


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
    added: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    """Where each kept step entered the main line, oldest first."""

    @classmethod
    def at(cls, repo: Path, revision: str) -> CarryChain:
        """The steps in the tree in the order the main line added them, and every step
        its history added and later deleted."""
        known = {}
        parents = carry_parents(git(repo, "show", f"{revision}:{FRAMEWORK}"))
        tree = git(repo, "ls-tree", "--name-only", f"{revision}:{STEPS}").decode().split()
        pending = {
            path.removesuffix(".py"): git(repo, "show", f"{revision}:{STEPS}/{path}")
            for path in tree if path.startswith("carry_") and path.endswith(".py")
        }
        while pending:
            declared = {
                name: Step.declared(name, source, revision, cls.source(repo, revision, name)[0],
                                    parents)
                for name, source in pending.items() if Step.holds_carry(source, parents)
            }
            if not declared:
                raise ValueError(
                    f"{sorted(pending)} holds no Carry step; "
                    "every carry_* module is a step of the chain")
            known.update(declared)
            parents.update({step.declaration: step.family for step in declared.values()})
            pending = {name: source for name, source in pending.items() if name not in declared}
        retired = cls.retired_at(repo, revision, known)
        retired_columns = frozenset(
            column for name in retired
            for column in Step.declared_columns(cls.source(repo, revision, name)[1]))
        added = cls.added_at(repo, revision, known)
        return cls(cls.ordered(sorted(known.values(), key=lambda step: added[step.name])),
                   retired, retired_columns, added)

    @staticmethod
    def ordered(steps: list[Step]) -> tuple[Step, ...]:
        """Steps in main-line order, each moved just before the first earlier step its
        family precedes."""
        order: list[Step] = []
        for step in steps:
            at = next((index for index, placed in enumerate(order)
                       if step.family.precedes(placed.family)), len(order))
            order.insert(at, step)
        for index, step in enumerate(order):
            for later in order[index + 1:]:
                if later.family.precedes(step.family):
                    raise ValueError(f"{later.name} must run before {step.name}, "
                                     "which must run before it")
        return tuple(order)

    @staticmethod
    def added_at(repo: Path, revision: str, kept: Mapping[str, Step]) -> dict[str, tuple[int, int]]:
        """Where each kept step entered the main line: the first-parent commit that added its
        file, then, for steps one merge brought in together, the order its merged side
        added them. Steps one commit adds together have no order, and are refused."""
        log = git(repo, "log", "-m", "--first-parent", "--no-renames", "--diff-filter=A",
                  "--reverse", "--name-only", "--format=%x00%H", revision, "--", str(STEPS))
        added: dict[str, tuple[int, int]] = {}
        for position, entry in enumerate(log.decode().split("\0")):
            fields = entry.split()
            names = [Path(path).stem for path in fields[1:]
                     if Path(path).stem in kept and Path(path).stem not in added]
            order = CarryChain.merged_order(repo, fields[0], names) if len(names) > 1 else names
            added.update({name: (position, index) for index, name in enumerate(order)})
        return added

    @staticmethod
    def merged_order(repo: Path, commit: str, names: list[str]) -> list[str]:
        """The order a merge's second parent added the steps the merge brings in together."""
        parents = git(repo, "rev-list", "--parents", "-n1", commit).decode().split()[1:]
        if len(parents) < 2:
            raise UnorderedSteps(commit, names)
        log = git(repo, "log", "--first-parent", "--no-renames", "--diff-filter=A", "--reverse",
                  "--name-only", "--format=%x00%H", f"{parents[0]}..{parents[1]}", "--", str(STEPS))
        order: list[str] = []
        for entry in log.decode().split("\0"):
            fields = entry.split()
            together = [Path(path).stem for path in fields[1:] if Path(path).stem in names]
            if len(together) > 1:
                raise UnorderedSteps(fields[0], together)
            order += together
        return order

    @staticmethod
    def retired_at(repo: Path, revision: str, kept: Mapping[str, Step]) -> frozenset[str]:
        """Step files the main line deleted. A finished step is retired by deleting its
        file; a bus that already ran it keeps the name in its ledger."""
        deleted = git(repo, "log", "-m", "--first-parent", "--no-renames", "--diff-filter=D",
                      "--name-only", "--format=", revision, "--", str(STEPS)).decode().split()
        return frozenset(Path(path).stem for path in deleted
                         if Path(path).parent == STEPS and Path(path).name.startswith("carry_")
                         and path.endswith(".py")) - set(kept)

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
        ``agent-comms install`` does; a ledger that pending() refuses stays refused."""
        if name in applied:
            return
        pending = [step.name for step in self.pending(applied)]
        if pending[:1] != [name]:
            ahead = pending[: pending.index(name) + 1] if name in pending else pending
            raise ValueError(f"the bus must first run {ahead}; run agent-comms install")

    def pending(self, applied: tuple[str, ...]) -> tuple[Step, ...]:
        """The steps a bus still needs. A recorded step must be kept or retired. A bus may
        lack an earlier step only when the main line added it after a later step the bus
        records, as a step its family moves ahead is; otherwise it was carried out of
        order, and is refused."""
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


class TodoColumnCarry(TodoRows):
    """Todo rows gain one declared record column, empty; every other column stays as it was."""

    column: ClassVar[str]

    @classmethod
    def precedes(cls, later: type[Carry]) -> bool:
        """Rows decode as today's Todo only once every declared column is there."""
        return issubclass(later, TodoRows) and not issubclass(later, TodoColumnCarry)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        chain = cls.chain()
        step = next(step for step in chain.steps if step.name == cls.name())
        return cls.carry_step(root, step, chain, apply=apply)

    @classmethod
    def carry_step(cls, root: Path, step: Step, chain: CarryChain, *, apply: bool) -> Carried:
        """One column owner applies its step's immutable declaration."""
        path = cls.store.path_in(root)
        if not path.exists():
            return Carried(0)
        with closing(CoordinationStore.writer(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = cls._carry(db, step, chain, apply=apply)
            except BaseException:
                db.rollback()
                raise
        rewritten = tuple(item.name for item in root.glob(f"{path.name}*"))
        return Carried(changed, {}, rewritten if changed and apply else ())

    @classmethod
    def _carry(cls, db: sqlite3.Connection, step: Step, chain: CarryChain, *, apply: bool) -> int:
        table, fields = Todo.declared_name, Todo._fields()
        actual = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
        expected = tuple(item.wire_name for item in fields)
        added = tuple(item for item in fields if item.wire_name not in actual)
        (column,) = step.columns
        if column not in {item.name for item in added}:
            # This step's column is there; the steps declaring the others add them.
            db.rollback()
            return 0
        # The chain says which declared columns this bus may still lack here; the
        # rebuild fills each one empty, as the step that declares it does.
        lacking = chain.lacking(step.name)
        if (actual != tuple(item.wire_name for item in fields if item not in added)
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
        saved = f"{table}_before_{column}"
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
