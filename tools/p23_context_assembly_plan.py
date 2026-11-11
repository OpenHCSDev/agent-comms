"""Move the existing Core-context assembly algorithm onto its actual Comms owner."""

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

    assembly = '''"""Core context assembly on the same owner as its goals, views, relations and bus."""

from agent_comms.execution.context_owner import ContextOwner
from agent_comms.context_segments.partner_activity import PartnerActivitySegment
from agent_comms.context_segments.relationships import RelationshipsSegment
from agent_comms.execution.turn_context import NextContextTurn, TurnContext
from agent_comms.history_views import HistoryViews
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.planning.goal_management import Goals
from agent_comms.relationships import ThreadRelationships


class ContextAssembly:
    __slots__ = ()

    goals: Goals
    views: HistoryViews
    relationships: ThreadRelationships
    bus: MessageBus

    def context_for(self, owner: ContextOwner) -> TurnContext:
        """Preview the next Core context, without recording or authorizing a native request."""
        relations = self.relationships.context_relations(owner.name)
        context = TurnContext.for_owner(
            owner, NextContextTurn(), "",
            tuple(view.thread for view in self.views.thread_views()), relations, self.goals,
        ).with_goal(owner, self.goals)
        for segment in (
            *RelationshipsSegment.capture(owner, relations),
            *self.bus.awareness_segments(owner, relations),
            *PartnerActivitySegment.capture(owner, relations, self.goals),
        ):
            context = context.append(segment)
        return context
'''
    operations.append({"operation": "create_file",
                       "file_path": str(args.root / "src/agent_comms/context_assembly.py"), "source": assembly})
    patch("src/agent_comms/comms.py", [
        {"old_source": "class Comms(ActivityClient):",
         "new_source": "class Comms(ActivityClient, ContextAssembly):"},
    ])
    operations.append({"operation": "ensure_import", "file_path": str(args.root / "src/agent_comms/comms.py"),
                       "import_source": "from agent_comms.execution.context_assembly import ContextAssembly"})
    patch("src/agent_comms/execution/turn_context.py", [
        {"old_source": '''    @classmethod
    def for_inspection(cls, comms, owner: ContextOwner) -> TurnContext:
        """Preview the Core context that would be added before the next input.

        Use the same context builders as turn preparation. This preview is not
        a recorded SDK request and does not grant permission to submit input.
        """
        relations = comms.relationships.context_relations(owner.name)
        context = cls.for_owner(owner, NextContextTurn(), "", tuple(view.thread for view in comms.views.thread_views()), relations, comms.goals)
        return context.with_goal(owner, comms.goals).with_awareness(comms, owner, relations)

''', "new_source": ""},
        {"old_source": '''    def with_awareness(
        self, comms, owner: ContextOwner, relations: tuple[PeerRelation, ...]
    ) -> TurnContext:
        """Append what the owner should know without being asked.

        Its relationships, the messages addressed to it and its partners' goal changes.
        """
        from agent_comms.context_segments.partner_activity import PartnerActivitySegment
        from agent_comms.context_segments.relationships import RelationshipsSegment

        context = self
        for segment in (
            *RelationshipsSegment.capture(owner, relations),
            *comms.bus.awareness_segments(owner, relations),
            *PartnerActivitySegment.capture(owner, relations, comms.goals),
        ):
            context = context.append(segment)
        return context

''', "new_source": ""},
    ])
    patch("src/agent_comms/runtime_requests.py", [
        {"old_source": "return TurnContext.for_inspection(comms, owner)",
         "new_source": "return comms.context_for(owner)"},
    ])
    for file in ("tests/test_context_source_inspection.py", "tests/test_peer_relevance.py", "tests/test_turn_context_wire.py"):
        before = (args.root / file).read_text()
        after = before.replace("TurnContext.for_inspection(comms, ", "comms.context_for(")
        assert before != after
        patch(file, [{"old_source": before, "new_source": after}])
    reason = (
        "TurnContext is the frozen value and already has a low for_owner constructor. Its "
        "inspection factory instead reads Comms.views/relationships/goals/bus and uses two "
        "local imports of concrete segment subclasses, making the low values depend back on "
        "their implementations. The actual Comms owns all four sources. Compose ContextAssembly "
        "on that same object, with no manager object, stored copy, new cache or forwarding "
        "compatibility method. Fold the sole-use with_awareness algorithm into context_for and "
        "delete both old value methods; migrate every indexed Core/test call. The source search "
        "finds no TurnContext subtype constructor or for_inspection override; the admitted calls "
        "all construct the original TurnContext. Preserve exact relation acquisition, full "
        "thread_views production and original thread projection, GoalSegment augmentation, "
        "eager Relationships/bus/Partner producer evaluation and segment append order. Keep all "
        "existing constructors and capture algorithms. This moves the algorithm onto its "
        "original state owner and fixes import direction; it does not claim that moving code "
        "alone lowers implementation facts. Counterpart/source visibility, current-preview "
        "and authenticated-source checks, frozen bytes and native authorization stay unchanged. "
        "Use ordinary runtime imports from the higher assembly owner, not local imports. "
        "Qualify all original context wire/inspection/peer tests, cold entries and actual "
        "Comms inheritance/runtime annotations. The historical provenance decoder closure is "
        "a separate preceding transaction and no result is claimed for it here."
    )
    args.plan.write_text(json.dumps({"recipes": [{"recipe_id": "p23-original-comms-context-assembly",
                                                "reason": reason, "operations": operations,
                                                "architecture_guards": [], "authority_claims": []}],
                                     "architecture_guards": []}, indent=2) + "\n")
    print(json.dumps({"operations": len(operations)}))


if __name__ == "__main__":
    main()
