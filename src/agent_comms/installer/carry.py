"""The steps a retained bus is carried through when its durable format changes.

A step is one module in ``tools/cutover`` holding one ``Carry``
subclass. Only ``agent-comms install`` runs it, in the runtime built from the target
revision with that revision's ``tools``, so every step runs against the source it is
installed with. A step is
idempotent: data it already carried reports no change, so a step's own dry run is the
one owner of whether a bus still needs it. Declared dependencies constrain the order
the main line added steps, starting at the first whose dry run reports a change.
An edited step is judged by what
it does now, never by its name. A finished step is retired by deleting its file once
every bus has run it.
"""
from __future__ import annotations

import inspect
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Annotated, ClassVar

from typing_extensions import TypeForm

from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import PathText
from agent_comms.installer.interpreter_operation import InterpreterOperation, RuntimeProbe
from agent_comms.locked_store import RootFile
from agent_comms.repo import Repo

STEPS = Path("tools/cutover")


@dataclass(frozen=True)
class Carried:
    """What one run of a step changed, or would change on a dry run."""

    changed: int
    detail: dict[str, int | bool] = field(default_factory=dict)
    rewritten: tuple[Annotated[Path, PathText], ...] = ()
    """Files under the bus root this run wrote or deleted; every other file stays
    byte-identical."""


@dataclass(frozen=True)
class Carry(DeclaredFamily, affix="", discovery_package="cutover"):
    """One migration's decoded source, destination and retirement location."""

    root: Annotated[Path, PathText]
    original_root: Annotated[Path, PathText]
    spill_dir: Annotated[Path, PathText]

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
        for path in paths:
            self.require_confined(self.root, path)
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
    def require_confined(root: Path, path: Path) -> None:
        """A carry must never follow a copied path or alias into the source bus."""
        if not path.is_relative_to(root) or path.resolve() != path:
            raise RelationViolationError(f"Carry refuses an unconfined path: {path}")


class ArchivedInputs:
    """Apply one carry input declaration to its retained snapshot roots as well."""

    @abstractmethod
    def roots(self) -> tuple[Path, ...]: ...

    def input_files(self) -> tuple[Path, ...]:
        paths: set[Path] = set()
        for root in self.roots():
            paths.update(super(ArchivedInputs, replace(self, root=root)).input_files())
        for path in paths:
            Carry.require_confined(self.root, path)
        return tuple(sorted(paths))


class CarryOperation[Answer](
    InterpreterOperation[tuple[Answer, ...], tuple[Carry, ...]],
    DeclaredFamily, affix="Operation",
):
    """One worker pass, with its answer type declared by the operation's run method."""

    @abstractmethod
    def run(self, steps: tuple[Carry, ...]) -> tuple[Answer, ...]: ...

@dataclass(frozen=True)
class MigrateOperation(CarryOperation[Carried]):
    apply: bool

    def run(self, steps: tuple[Carry, ...]) -> tuple[Carried, ...]:
        return tuple(step.carry(apply=self.apply) for step in steps)


@dataclass(frozen=True)
class FirstPendingOperation(CarryOperation[int]):
    """One position: stop before asking a step that needs a pending predecessor."""

    def run(self, steps: tuple[Carry, ...]) -> tuple[int, ...]:
        for position, step in enumerate(steps):
            changed_count = step.carry(apply=False).changed
            if changed_count:
                return (position,)
        return (len(steps),)


@dataclass(frozen=True)
class RetireInputsOperation(CarryOperation[Carried]):
    def run(self, steps: tuple[Carry, ...]) -> tuple[Carried, ...]:
        return tuple(step.after_acceptance() for step in steps)


@dataclass(frozen=True)
class CarryProbe[Answer](RuntimeProbe[tuple[Answer, ...]]):
    """One pass of steps, in order, answered by the staged runtime's worker; the
    operation owns traversal and declares the answer."""

    operation: CarryOperation
    steps: tuple[Carry, ...]

    def answer_type(self) -> TypeForm[tuple[Answer, ...]]:
        return self.operation.answer_type()

    def run(self) -> tuple[Answer, ...]:
        return self.operation.run(self.steps)


@dataclass(frozen=True)
class CarryChain:
    """Every declared step, dependency order first and addition order for independent steps."""

    steps: tuple[type[Carry], ...]

    @classmethod
    def at(cls, tree: Path) -> CarryChain:
        """Import the tree's steps (its ``tools`` is on the path) and order them by the
        first-parent commit that added each file."""
        tree = tree.resolve()
        directory = tree / STEPS
        steps = [step for step in Carry.members_with(Carry) if step.module().parent == directory]
        log = Repo.git(tree, "log", "-m", "--first-parent", "--no-renames", "--diff-filter=A",
                       "--reverse", "--name-only", "--format=", "HEAD", "--", str(STEPS)).split()
        added = {Path(path): position for position, path in reversed(list(enumerate(log)))}
        unordered = sorted(step.declared_name for step in steps
                           if step.module().relative_to(tree) not in added)
        if unordered:
            raise ValueError(f"{unordered} are not on the main line, so nothing orders them")
        ordered = sorted(steps, key=lambda step: added[step.module().relative_to(tree)])
        dependencies: dict[type[Carry], set[type[Carry]]] = {step: set() for step in ordered}
        for step in ordered:
            for later in step.precedes:
                if later in dependencies:
                    dependencies[later].add(step)
        sorter = TopologicalSorter(dependencies)
        sorter.prepare()
        emitted = []
        ready = set()
        while sorter.is_active():
            ready.update(sorter.get_ready())
            step = next(step for step in ordered if step in ready)
            ready.remove(step)
            emitted.append(step)
            sorter.done(step)
        return cls(tuple(emitted))
