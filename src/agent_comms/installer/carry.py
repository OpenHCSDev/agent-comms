"""The steps a retained bus is carried through when its durable format changes.

A step is one module in ``tools/cutover``, ``carry_<what>.py``, holding one ``Carry``
subclass. Only ``agent-comms install`` runs it, with the target revision's ``src`` and
``tools``, so every step runs against the source it is installed with. A step is
idempotent: data it already carried reports no change, so a step's own dry run is the
one owner of whether a bus still needs it. Steps run in the order the main line added
them, from the first whose dry run reports a change. An edited step is judged by what
it does now, never by its name. A finished step is retired by deleting its file once
every bus has run it.
"""
from __future__ import annotations

import inspect
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.field_codec import PathText
from agent_comms.errors import RelationViolationError

STEPS = Path("tools/cutover")


@dataclass(frozen=True)
class Carried:
    """What one run of a step changed, or would change on a dry run."""

    changed: int
    detail: dict[str, int | bool] = field(default_factory=dict)
    rewritten: tuple[str, ...] = ()
    """Files under the bus root this run wrote; every other file stays byte-identical."""


class Carry(ABC):
    declared: ClassVar[dict[str, type[Carry]]] = {}
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

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Annotated[Path, PathText], ...]:
        raise RuntimeError(f'{cls.name()} needs the full agent-comms install operation')

    @classmethod
    @abstractmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        """Carry ``root`` with its wire lock held; on a dry run, write nothing."""

    @staticmethod
    def require_confined(root: Path, path: Path) -> None:
        """A carry must never follow a copied path or alias into the source bus."""
        if not path.is_relative_to(root) or path.resolve() != path:
            raise RelationViolationError(f"Carry refuses an unconfined path: {path}")

    @classmethod
    def before_stop(cls, root: Path, *, apply: bool) -> Carried:
        """What the running installation needs before its own stop command can read the bus.
        It records nothing in the ledger."""
        return Carried(0)


@dataclass(frozen=True)
class CarryChain:
    """Every step a tree declares, in the order the main line added them."""

    steps: tuple[type[Carry], ...]

    @classmethod
    def at(cls, tree: Path) -> CarryChain:
        """Import the tree's steps (its ``tools`` is on the path) and order them by the
        first-parent commit that added each file."""
        directory = (tree / STEPS).resolve()
        for path in sorted(directory.glob("carry_*.py")):
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
        return cls(tuple(sorted(steps, key=lambda step: added[step.name()])))

    def pending(self, dry_run: Callable[[type[Carry]], Carried]) -> tuple[type[Carry], ...]:
        """Every step from the first whose dry run reports a change. A later step's dry run
        reads what the earlier ones leave, so it is not asked before they have run; it runs
        after them and reports no change if it has none."""
        for position, step in enumerate(self.steps):
            if dry_run(step).changed:
                return self.steps[position:]
        return ()

