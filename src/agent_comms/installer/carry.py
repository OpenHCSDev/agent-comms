"""Order the declared carry steps by Git history and their dependency declarations."""

from __future__ import annotations

from dataclasses import dataclass
from graphlib import TopologicalSorter
from pathlib import Path

from agent_comms.installer.probes.carry import Carry
from agent_comms.repo import Repo

STEPS = Path("tools/cutover")


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
        log = Repo.git(
            tree,
            "log",
            "-m",
            "--first-parent",
            "--no-renames",
            "--diff-filter=A",
            "--reverse",
            "--name-only",
            "--format=",
            "HEAD",
            "--",
            str(STEPS),
        ).split()
        added = {Path(path): position for position, path in reversed(list(enumerate(log)))}
        unordered = sorted(
            step.declared_name for step in steps if step.module().relative_to(tree) not in added
        )
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
