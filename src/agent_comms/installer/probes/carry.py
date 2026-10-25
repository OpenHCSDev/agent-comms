"""Carry declarations and operations executed by the selected runtime worker."""

from __future__ import annotations

import inspect
import sqlite3
from abc import abstractmethod
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, ClassVar

from typing_extensions import TypeForm

from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import PathText
from agent_comms.installer.interpreter_operation import (
    InstallClock,
    InterpreterOperation,
    RuntimeProbe,
    SubjectPhase,
    TargetProbe,
)
from agent_comms.locked_store import FilesRequest, RootFile
from agent_comms.store_files import BorrowedStoreLock, is_store_lock
from agent_comms.typed_table import TriggerKind, TypedTable, WriterOwnedStore, _Field


@dataclass(frozen=True)
class Carried:
    """What one run of a step changed, or would change on a dry run."""

    changed: int
    detail: dict[str, int | bool] = field(default_factory=dict)
    rewritten: tuple[Annotated[Path, PathText], ...] = ()
    """Bus-relative names or declared writer-owned paths this run wrote or deleted.
    Store locks are excluded from that inventory and cannot authorize a rewrite."""

    def for_step(self, step: Carry) -> Carried:
        resources = tuple(item for path in self.rewritten
                          for item in RootFile.referenced_files(
                              step.files, path))
        for resource in resources:
            step.require_confined(step.files.location(resource))
        return replace(self, rewritten=tuple(resource.path for resource in resources))

    def __post_init__(self) -> None:
        for path in self.rewritten:
            if is_store_lock(path):
                raise TypeError(
                    f"Carried.rewritten cannot name an inventory-excluded store lock: {path}"
                )


@dataclass(frozen=True)
class Carry(DeclaredFamily, affix="", discovery_package="cutover"):
    """One migration's decoded source, destination and retirement location."""

    files: FilesRequest
    spill_dir: Annotated[Path, PathText]

    @property
    def root(self) -> Path:
        return self.files.root

    writer_owned: ClassVar[tuple[type[WriterOwnedStore], ...]] = ()
    """External durable declarations this step is allowed to read and carry."""

    @staticmethod
    def writer_declarations(steps: Iterable[Carry | type[Carry]]) -> tuple[type[WriterOwnedStore], ...]:
        return tuple(dict.fromkeys(store for step in steps for store in step.writer_owned))

    precedes: ClassVar[tuple[type[Carry], ...]] = ()
    """Steps whose decoders require this step's output, regardless of addition date."""

    @property
    @abstractmethod
    def opened_files(self) -> tuple[type[RootFile], ...]:
        """The store owners this carry reads or writes, declared by every member."""

    def input_files(self) -> tuple[Path, ...]:
        """Enumerate declared inputs without opening any store with the staged runtime."""
        paths: set[Path] = set()
        for owner, directory in RootFile.declarations_in(self.root):
            if issubclass(owner, self.opened_files):
                paths.update(owner.matching_in(directory))
        paths.update(self.files.location(resource) for store in self.writer_owned
                     for resource in store.resources(self.files))
        for path in paths:
            self.require_confined(path)
        return tuple(sorted(paths))

    @classmethod
    def module(cls) -> Path:
        return Path(inspect.getfile(cls)).resolve()

    @abstractmethod
    def carry(self, *, apply: bool) -> Carried:
        """Carry this root; on a dry run, write nothing."""

    def after_acceptance(self) -> Carried:
        """Retire obsolete inputs after the installation's rollback window closes."""
        return Carried(0)

    @staticmethod
    def rewrite_stored(
        db: sqlite3.Connection,
        row: type[TypedTable],
        column: _Field,
        updates: Iterable[tuple[object, object]],
    ) -> None:
        """Inside the caller's write transaction, carry stored values of a guarded row:
        its declared mutation guards are lifted and recreated from the same declaration."""
        (key,) = (field.name for field in row._fields if field.column.primary_key)
        guards = row.triggers()
        for name in guards:
            TriggerKind.drop(db, name)
        db.executemany(
            row._sql(f'UPDATE "{row.declared_name}" SET {{{column.name}}}=? WHERE {{{key}}}=?'),
            updates,
        )
        for sql in guards.values():
            db.execute(sql)

    def require_confined(self, path: Path) -> None:
        """A carry reaches its bus and exactly its declared external files, never aliases."""
        external = {self.files.location(resource) for store in self.writer_owned
                    for resource in store.resources(self.files)}
        if (not path.is_relative_to(self.root) and path not in external) or path.resolve() != path:
            raise RelationViolationError(f"Carry refuses an unconfined path: {path}")


class CarryOperation[Answer](
    InterpreterOperation[tuple[Answer, ...], tuple[Carry, ...]],
    DeclaredFamily,
    affix="Operation",
):
    """One worker pass, with its answer type declared by the operation's run method."""

    @abstractmethod
    def run(self, steps: tuple[Carry, ...]) -> tuple[Answer, ...]: ...


@dataclass(frozen=True, kw_only=True)
class CarryPhase(SubjectPhase):
    """Run a declared carry operation in its target interpreter."""

    operation: type[CarryOperation]

    @property
    def subject(self) -> str:
        return self.operation.declared_name


@dataclass(frozen=True, kw_only=True)
class CarryStepPhase(SubjectPhase):
    """One carry step inside its operation's worker pass."""

    step: type[Carry]

    @property
    def subject(self) -> str:
        return self.step.declared_name


class CarriedOperation(CarryOperation[Carried]):
    @abstractmethod
    def answer(self, step: Carry) -> Carried: ...

    def run(self, steps: tuple[Carry, ...]) -> tuple[Carried, ...]:
        answers = []
        for step in steps:
            with CarryStepPhase(step=type(step)).running(InstallClock.current.get()):
                answers.append(self.answer(step).for_step(step))
        return tuple(answers)


@dataclass(frozen=True)
class MigrateOperation(CarriedOperation):
    apply: bool

    def answer(self, step: Carry) -> Carried:
        return step.carry(apply=self.apply)


@dataclass(frozen=True)
class FirstPendingOperation(CarryOperation[int]):
    """One position: stop before asking a step that needs a pending predecessor."""

    def run(self, steps: tuple[Carry, ...]) -> tuple[int, ...]:
        for position, step in enumerate(steps):
            with CarryStepPhase(step=type(step)).running(InstallClock.current.get()):
                changed_count = step.carry(apply=False).changed
            if changed_count:
                return (position,)
        return (len(steps),)


@dataclass(frozen=True)
class RetireInputsOperation(CarriedOperation):
    def answer(self, step: Carry) -> Carried:
        return step.after_acceptance()


@dataclass(frozen=True)
class WriterOwnedOpeningProbe(TargetProbe, RuntimeProbe[tuple[Annotated[Path, PathText], ...]]):
    """Validate carried external rows in the target, at the selected live or copied paths."""

    files: FilesRequest
    stores: tuple[type[WriterOwnedStore], ...]

    def run(self) -> tuple[Annotated[Path, PathText], ...]:
        paths = []
        with TemporaryDirectory() as directory:
            for store in self.stores:
                store.practice_opening(self.files, Path(directory))
                paths.extend(self.files.location(resource)
                             for resource in store.resources(self.files))
        return tuple(paths)


@dataclass(frozen=True)
class CarryProbe[Answer](TargetProbe, RuntimeProbe[tuple[Answer, ...]]):
    """A typed carry pass; the common worker exchange owns its measurements."""

    operation: CarryOperation
    steps: tuple[Carry, ...]
    inherited_locks: tuple[BorrowedStoreLock, ...] = ()

    def install_phase(self) -> CarryPhase:
        return CarryPhase(operation=type(self.operation))

    def answer_type(self) -> TypeForm[tuple[Answer, ...]]:
        return self.operation.answer_type()

    def run(self) -> tuple[Answer, ...]:
        with ExitStack() as lifetime:
            for lock in self.inherited_locks:
                lifetime.enter_context(lock.joined())
            return self.operation.run(self.steps)
