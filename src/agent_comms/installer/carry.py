"""The steps a retained bus is carried through when its durable format changes.

A step is one module in ``tools/cutover``, ``carry_<what>.py``, holding one ``Carry``
subclass. Only ``agent-comms install`` runs it, with the target revision's ``src`` and
``tools``, so every step runs against the source it is installed with. A step is
idempotent: data it already carried reports no change. Pending steps run in the order
the main line added them. A finished step is retired by deleting its file once every
bus has run it; a ledger keeps the names of the steps it ran, deleted or not.

Each bus records the steps applied to it in ``carries.json``. Only this family
reads or writes that file; nothing else in ``src/`` depends on it.
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.field_codec import PathText

STEPS = Path("tools/cutover")


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

    def pending(self, applied: tuple[str, ...]) -> tuple[type[Carry], ...]:
        """A recorded name with no declared step is a finished step whose file was deleted."""
        return tuple(step for step in self.steps if step.name() not in applied)


@dataclass(frozen=True)
class CarryLedger:
    """The steps applied to one bus, oldest first."""

    root: Path

    name: ClassVar[str] = "carries.json"

    @property
    def path(self) -> Path:
        return self.root / self.name

    def applied(self) -> tuple[str, ...]:
        if not self.path.exists():
            return ()
        return tuple(json.loads(self.path.read_text())["applied"])

    def apply(self, name: str, operation: Callable[[], Carried]) -> Carried:
        """The caller holds the wire lock across conversion and recording."""
        if name in self.applied():
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
        self.write(tuple(step.name() for step in chain.steps))

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
