"""Shared relation behavior extends without endpoint-comparison consumer edits."""

from agent_comms.relationships import MutualThreadRelation
from agent_comms.threads import Thread


def test_new_relation_reuses_owned_incident_and_counterpart_behavior():
    first = Thread("first", frozenset(), "/wt", created_at=1)
    second = Thread("second", frozenset(), "/wt", created_at=2)
    replacement = Thread("second", frozenset(), "/wt", created_at=3)

    class ReviewRelation(MutualThreadRelation):
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
