"""Shared relation behavior extends without endpoint-comparison consumer edits."""
from agent_comms.storage_rules import StorageRule

from agent_comms.peer_relevance import GoalContactRelevance
from agent_comms.relationships import MutualThreadRelation, RelationRecord
from agent_comms.threads import Thread


def test_new_relation_reuses_owned_incident_and_counterpart_behavior():
    first = Thread.create("first", frozenset(), "/wt", created_at=1, storage_rule=StorageRule())
    second = Thread.create("second", frozenset(), "/wt", created_at=2, storage_rule=StorageRule())
    replacement = Thread.create("second", frozenset(), "/wt", created_at=3, storage_rule=StorageRule())

    class ReviewRelation(MutualThreadRelation):
        relevance = GoalContactRelevance
        note = "Reviewing"

        def record(self, peer):
            return RelationRecord(peer.name, self.relevance, first.name, None, self.note, ())

        @property
        def owner_incarnation(self):
            return first.incarnation

        @property
        def peer_incarnation(self):
            return second.incarnation

    relation = ReviewRelation()
    assert relation.incident(first) and relation.incident(second)
    assert relation.counterpart(first) == second.incarnation
    assert relation.counterpart(second) == first.incarnation
    assert not relation.incident(replacement)
    assert relation.counterpart(replacement) is None
    assert relation.record(second.incarnation).peer == "second"
