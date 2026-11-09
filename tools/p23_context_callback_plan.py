"""Prepare the next context callback cut and delete the unused retention facet."""

import argparse
import json
from pathlib import Path

from pydantic import JsonValue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    operations: list[JsonValue] = []

    def patch(file: str, replacements: list[dict[str, str]]) -> None:
        operations.append({"operation": "patch_target", "file_path": str(args.root / file),
                           "replacements": replacements})

    def ensure(file: str, text: str) -> None:
        operations.append({"operation": "ensure_import", "file_path": str(args.root / file),
                           "import_source": text})

    source = '''"""An original input supplies its own determining context provenance."""

from abc import ABC, abstractmethod


class ContextProvenanceSource[Source](ABC):
    __slots__ = ()

    @abstractmethod
    def context_provenance(self) -> Source: ...
'''
    operations.append({"operation": "create_file",
                       "file_path": str(args.root / "src/agent_comms/context_provenance_source.py"),
                       "source": source})
    patch("src/agent_comms/input_attempt.py", [
        {"old_source": "class StoredInput(InputAttempt):",
         "new_source": "class StoredInput(InputAttempt, ContextProvenanceSource[InputProvenance]):"},
    ])
    ensure("src/agent_comms/input_attempt.py",
           "from agent_comms.execution.context_provenance_source import ContextProvenanceSource")
    patch("src/agent_comms/execution/turn_context.py", [
        {"old_source": "from agent_comms.execution.context_owner import ContextOwner",
         "new_source": "from agent_comms.execution.context_owner import ContextOwner\nfrom agent_comms.execution.context_provenance_source import ContextProvenanceSource"},
        {"old_source": "from agent_comms.peer_relevance import AlwaysListed, GoalShown, PeerRelevance",
         "new_source": "from agent_comms.peer_relevance import AlwaysListed, PeerRelevance"},
        {"old_source": "from typing import TYPE_CHECKING, Annotated, Self",
         "new_source": "from typing import Annotated, Self"},
        {"old_source": "if TYPE_CHECKING:\n    from agent_comms.input_attempt import StoredInput\n    from agent_comms.presentation import ThreadView\n",
         "new_source": ""},
        {"old_source": "    def from_view(cls, view: ThreadView, relation: PeerRelation, goals) -> PeerState:\n        goal = view.thread.goal.active() if relation.relevance.grants(GoalShown) else NoGoal()\n        return cls(\n            view.thread.name,\n            relation.relevance,\n            relation.note,\n            goal.instructions_for(goals, view.thread.incarnation),\n        )",
         "new_source": "    def from_owner(cls, owner: ContextOwner, relation: PeerRelation, goals) -> PeerState:\n        goal = relation.relevance.context_goal(owner.goal.active, NoGoal)\n        return cls(\n            owner.name,\n            relation.relevance,\n            relation.note,\n            goal.instructions_for(goals, owner.incarnation),\n        )"},
        {"old_source": "        cls, owner: ContextOwner, views: tuple[ThreadView, ...], relations: tuple[PeerRelation, ...], goals\n",
         "new_source": "        cls, owner: ContextOwner, peer_owners: tuple[ContextOwner, ...], relations: tuple[PeerRelation, ...], goals\n"},
        {"old_source": "        viewed = {view.thread.incarnation: view for view in views}\n        peers = tuple(PeerState.from_view(viewed[r.peer], r, goals)\n                      for r in relations if r.peer in viewed and r.relevance.grants(AlwaysListed))",
         "new_source": "        by_incarnation = {peer.incarnation: peer for peer in peer_owners}\n        peers = tuple(PeerState.from_owner(by_incarnation[r.peer], r, goals)\n                      for r in relations if r.peer in by_incarnation and r.relevance.grants(AlwaysListed))"},
        {"old_source": '        context = cls.for_owner(owner, NextContextTurn(), "", comms.views.thread_views(), relations, comms.goals)',
         "new_source": '        context = cls.for_owner(owner, NextContextTurn(), "", tuple(view.thread for view in comms.views.thread_views()), relations, comms.goals)'},
        {"old_source": "        views: tuple[ThreadView, ...],",
         "new_source": "        peer_owners: tuple[ContextOwner, ...],"},
        {"old_source": "        inputs: tuple[StoredInput, ...] = (),",
         "new_source": "        inputs: tuple[ContextProvenanceSource[Provenance], ...] = (),"},
        {"old_source": "                CoordinationSegment.capture(owner, views, relations, goals),",
         "new_source": "                CoordinationSegment.capture(owner, peer_owners, relations, goals),"},
    ])
    patch("src/agent_comms/execution/owned_turn.py", [
        {"old_source": "            self.runner.comms.views.thread_views(),",
         "new_source": "            tuple(view.thread for view in self.runner.comms.views.thread_views()),"},
    ])
    patch("tests/test_peer_relevance.py", [
        {"old_source": '        owner, NextContextTurn(), "", comms.views.thread_views(),',
         "new_source": '        owner, NextContextTurn(), "", tuple(view.thread for view in comms.views.thread_views()),'},
        {"old_source": "        owner, comms.views.thread_views(), comms.relationships.context_relations(name), comms.goals",
         "new_source": "        owner, tuple(view.thread for view in comms.views.thread_views()), comms.relationships.context_relations(name), comms.goals"},
    ])
    patch("src/agent_comms/peer_relevance.py", [
        {"old_source": "from __future__ import annotations\n", "new_source": ""},
        {"old_source": "from collections.abc import Sequence", "new_source": "from collections.abc import Callable, Sequence"},
        {"old_source": "class GoalShown(RelevanceCapability):\n    \"\"\"The peer list says what the peer is working on now.\"\"\"",
         "new_source": "class PeerGoalProjection:\n    \"\"\"Hidden goals are the default; GoalShown supplies the one differing behavior.\"\"\"\n\n    @classmethod\n    def context_goal[Goal](cls, active: Callable[[], Goal], hidden: Callable[[], Goal]) -> Goal:\n        return hidden()\n\n\nclass GoalShown(RelevanceCapability, PeerGoalProjection):\n    \"\"\"The peer list says what the peer is working on now.\"\"\"\n\n    @classmethod\n    def context_goal[Goal](cls, active: Callable[[], Goal], hidden: Callable[[], Goal]) -> Goal:\n        return active()"},
        {"old_source": 'class PeerRelevance(DeclaredFamily, affix="Relevance"):',
         "new_source": 'class PeerRelevance(DeclaredFamily, PeerGoalProjection, affix="Relevance"):'},
    ])
    patch("src/agent_comms/messages.py", [
        {"old_source": "    @property\n    def retains_authored_task(self) -> bool:\n        return self.task.retains_authored_task\n\n", "new_source": ""},
    ])
    patch("src/agent_comms/task_sources.py", [
        {"old_source": "    retains_authored_task = False\n", "new_source": ""},
        {"old_source": "    retains_authored_task = True\n", "new_source": ""},
    ])
    reason = (
        "The original context callbacks consume only peer name/goal/incarnation from the "
        "original Thread and context_provenance from stored input. Reuse ContextOwner instead "
        "of importing complete presentation for those facts. Preserve the exact existing "
        "thread_views production, order, snapshot/goal/activity work and related exceptions at "
        "each caller, then pass its actual immutable thread values. Do not replace roster "
        "production with a fresh registry cut or infer that its extra work is unnecessary. "
        "ContextProvenanceSource is a lower generic ABC inherited by the actual StoredInput; "
        "retain its method, original InputProvenance object, keys, origins and all fences. "
        "The existing GoalShown capability owns active-goal selection; PeerGoalProjection "
        "supplies the hidden default shared by relevance and the capability. Its shared "
        "ancestor places GoalShown's override before that default without changing existing "
        "relevance or RelevanceCapability memberships. Preserve current/hidden construction "
        "and final goal instructions; native Python 3.14 scopes retain the callback result type. "
        "The bounded Core/Toad src/tests/tools search finds no retention-facet reader except "
        "Message's wrapper and the two unannotated class attributes. They are not dataclass "
        "fields and FieldCodec does not export them. Delete the dead facet with no negative "
        "name-pinning test. Arbitrary external reflection is outside this private admitted "
        "corpus, not a guessed preservation certificate. Authored semantic edits need actual "
        "MRO, peer visibility/goal, native input/context byte and cold callback qualification. "
        "This removes the two guarded context imports; the three existing local imports in "
        "turn_context remain explicit OPEN dependency work, not a claimed whole-module closure."
    )
    args.plan.write_text(json.dumps({"recipes": [{"recipe_id": "p23-context-original-owner-and-source-contracts",
                                                "reason": reason, "operations": operations,
                                                "architecture_guards": [], "authority_claims": []}],
                                     "architecture_guards": []}, indent=2) + "\n")
    print(json.dumps({"operations": len(operations)}))


if __name__ == "__main__":
    main()
