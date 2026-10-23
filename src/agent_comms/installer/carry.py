"""The steps a retained bus is carried through when its durable format changes.

A step is one module in ``tools/cutover`` holding one ``Carry``
subclass. Only ``agent-comms install`` runs it, with the target revision's ``src`` and
``tools``, so every step runs against the source it is installed with. A step is
idempotent: data it already carried reports no change, so a step's own dry run is the
one owner of whether a bus still needs it. Declared dependencies constrain the order
the main line added steps, starting at the first whose dry run reports a change.
An edited step is judged by what
it does now, never by its name. A finished step is retired by deleting its file once
every bus has run it.
"""
from __future__ import annotations

import inspect
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from importlib import import_module
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import PathText
from agent_comms.installer.interpreter_operation import InterpreterOperation

STEPS = Path("tools/cutover")


@dataclass(frozen=True)
class Carried:
    """What one run of a step changed, or would change on a dry run."""

    changed: int
    detail: dict[str, int | bool] = field(default_factory=dict)
    rewritten: tuple[str, ...] = ()
    """Files under the bus root this run wrote; every other file stays byte-identical."""


@dataclass(frozen=True)
class Carry(ABC):
    """One migration's decoded source, destination and retirement location."""

    root: Annotated[Path, PathText]
    original_root: Annotated[Path, PathText]
    spill_dir: Annotated[Path, PathText]

    declared: ClassVar[dict[str, type[Carry]]] = {}
    precedes: ClassVar[tuple[type[Carry], ...]] = ()
    """Steps whose decoders require this step's output, regardless of addition date."""
    """Every step imported so far, by its module name."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if inspect.isabstract(cls):
            return
        if cls.name() in Carry.declared and Carry.declared[cls.name()] is not cls:
            raise TypeError(f"{cls.name()} declares a second carry step")
        Carry.declared[cls.name()] = cls

    @classmethod
    def module(cls) -> Path:
        return Path(inspect.getfile(cls)).resolve()

    @classmethod
    def name(cls) -> str:
        return cls.module().stem

    @abstractmethod
    def backup_files(self) -> tuple[Annotated[Path, PathText], ...]:
        """The existing files this step rewrites or deletes, backed up before it applies."""

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

class CarryOperation[Answer](InterpreterOperation[Answer, Carry], DeclaredFamily, affix="Operation"):
    """One step invocation, including its arguments and its declared answer type."""

    @abstractmethod
    def run(self, step: Carry) -> Answer: ...

@dataclass(frozen=True)
class BackupFilesOperation(CarryOperation[tuple[Annotated[Path, PathText], ...]]):
    def run(self, step: Carry) -> tuple[Annotated[Path, PathText], ...]:
        return step.backup_files()


@dataclass(frozen=True)
class MigrateOperation(CarryOperation[Carried]):
    apply: bool

    def run(self, step: Carry) -> Carried:
        return step.carry(apply=self.apply)


@dataclass(frozen=True)
class RetireInputsOperation(CarryOperation[Carried]):
    def run(self, step: Carry) -> Carried:
        return step.after_acceptance()


@dataclass(frozen=True)
class CarryChain:
    """Every declared step, dependency order first and addition order for independent steps."""

    steps: tuple[type[Carry], ...]

    @classmethod
    def at(cls, tree: Path) -> CarryChain:
        """Import the tree's steps (its ``tools`` is on the path) and order them by the
        first-parent commit that added each file."""
        directory = (tree / STEPS).resolve()
        for path in sorted(directory.glob("*.py")):
            import_module(f"{directory.name}.{path.stem}")
        steps = [step for step in Carry.declared.values() if step.module().parent == directory]
        log = subprocess.check_output(
            ["git", "-C", str(tree), "log", "-m", "--first-parent", "--no-renames",
             "--diff-filter=A", "--reverse", "--name-only", "--format=", "HEAD", "--",
             str(STEPS)]).decode().split()
        added = {Path(path).stem: position for position, path in reversed(list(enumerate(log)))}
        unordered = sorted(step.name() for step in steps if step.name() not in added)
        if unordered:
            raise ValueError(f"{unordered} are not on the main line, so nothing orders them")
        ordered = sorted(steps, key=lambda step: added[step.name()])
        dependencies: dict[type[Carry], set[type[Carry]]] = {step: set() for step in ordered}
        for step in ordered:
            for later in step.precedes:
                if later in dependencies:
                    dependencies[later].add(step)
        return cls(tuple(TopologicalSorter(dependencies).static_order()))

    def pending(self, dry_run: Callable[[type[Carry]], Carried]) -> tuple[type[Carry], ...]:
        """Every step from the first whose dry run reports a change. A later step's dry run
        reads what the earlier ones leave, so it is not asked before they have run; it runs
        after them and reports no change if it has none."""
        for position, step in enumerate(self.steps):
            if dry_run(step).changed:
                return self.steps[position:]
        return ()
