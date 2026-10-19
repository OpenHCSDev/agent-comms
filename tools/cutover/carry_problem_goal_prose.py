"""Problem goals drop the text and progress their Todos own (P147).

A problem goal's text is ProblemGoalOrder's fixed text and its progress is read
from its Todos each turn. Each problem goal still holding its own prose takes the
fixed text at a new revision, with its mention source rebound to that text.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from cutover.carry import Carried, Carry

from agent_comms.goals import Goal, ProblemGoalOrder
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.text_digest import TextDigest


class ProblemGoalProse(Carry):
    """Each problem goal stores the fixed problem text and no progress."""

    after = "carry_goal_wait_targets"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore(root / "registry.json")
        if not store.path.exists():
            return Carried(0)
        changed = 0
        with store.locked():
            document = RegistryDocument.from_wire(json.loads(store.path.read_text()))
            for name, thread in document.threads.items():
                goal = thread.goal
                if not isinstance(goal, Goal) or not isinstance(goal.order, ProblemGoalOrder):
                    continue
                if (goal.text, goal.progress) == (ProblemGoalOrder.text, ""):
                    continue
                changed += 1
                revision = goal.revision + 1
                text = replace(goal, text=ProblemGoalOrder.text, progress="", revision=revision)
                if goal.mention_source is not None:
                    digest = TextDigest.of(ProblemGoalOrder.text).value
                    source = replace(
                        goal.mention_source, text_revision=revision, text_digest=digest, bindings=()
                    )
                    text = replace(text, mention_source=source)
                document.threads[name] = replace(thread, goal=text)
            if apply and changed:
                store.save_unlocked(document)
        written = (store.path.name, store.guard_path.name) if changed and apply else ()
        return Carried(changed, {"threads": len(document.threads)}, written)


if __name__ == "__main__":
    ProblemGoalProse.main()
