"""Problem goals drop the text and progress their Todos own (P147).

A problem goal's text is ProblemGoalOrder's fixed text and its progress is read
from its Todos each turn, so a stored copy only repeats a wait or block the
Todos no longer hold. Each problem goal still holding its own prose takes the
fixed text at a new revision, with its mention source rebound to that text.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from agent_comms.goals import Goal, ProblemGoalOrder
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.text_digest import TextDigest

from cutover.carry import Carried, Carry


class ProblemGoalProse(Carry):
    """Each problem goal stores the fixed problem text and no progress."""

    after = "carry_goal_wait_targets"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore(root / "registry.json")
        if not store.path.exists():
            return Carried(0)
        with store.locked():
            document = RegistryDocument.from_wire(json.loads(store.path.read_text()))
            prose = [name for name, thread in document.threads.items()
                     if isinstance(thread.goal, Goal) and isinstance(thread.goal.order, ProblemGoalOrder)
                     and (thread.goal.text, thread.goal.progress) != (ProblemGoalOrder.text, "")]
            for name in prose:
                thread = document.threads[name]
                goal = thread.goal
                revision = goal.revision + 1
                source = goal.mention_source and replace(
                    goal.mention_source, text_revision=revision, bindings=(),
                    text_digest=TextDigest.of(ProblemGoalOrder.text).value)
                document.threads[name] = replace(thread, goal=replace(
                    goal, text=ProblemGoalOrder.text, progress="", revision=revision,
                    mention_source=source))
            if apply and prose:
                store.save_unlocked(document)
        written = (store.path.name, store.guard_path.name) if prose and apply else ()
        return Carried(len(prose), {"threads": len(document.threads)}, written)


if __name__ == "__main__":
    ProblemGoalProse.main()
