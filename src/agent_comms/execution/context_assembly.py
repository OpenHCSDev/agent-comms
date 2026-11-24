"""Core context assembly on the same owner as its goals, views, relations and bus."""

from agent_comms.context_segments.partner_activity import PartnerActivitySegment
from agent_comms.context_segments.relationships import RelationshipsSegment
from agent_comms.execution.context_owner import ContextOwner
from agent_comms.execution.turn_context import NextContextTurn, TurnContext
from agent_comms.history_views import HistoryViews
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.planning.goal_management import Goals
from agent_comms.registry.peer_relation import PeerRelation
from agent_comms.registry.relationships import ThreadRelationships
from agent_comms.registry.roster_visibility import DEFAULT_ROSTER


class ContextAssembly:
    __slots__ = ()

    goals: Goals
    views: HistoryViews
    relationships: ThreadRelationships
    bus: MessageBus

    def context_for(self, owner: ContextOwner) -> TurnContext:
        """Preview the next Core context, without recording or authorizing a native request."""
        registry = self.views.registry.snapshot()
        current = registry.require(owner.name)
        relations = self.relationships.context_relations_at(registry, current)
        context = TurnContext.for_owner(
            current, NextContextTurn(), "",
            tuple(registry.visible_threads(DEFAULT_ROSTER)), relations, self.goals,
        ).with_goal(current, self.goals)
        return self.context_with_awareness(context, current, relations)

    def context_with_awareness(
        self, context: TurnContext, owner: ContextOwner, relations: tuple[PeerRelation, ...],
    ) -> TurnContext:
        """Append relationships, addressed messages and partner goal changes."""
        segments = list(self.bus.awareness_segments(owner, relations))
        if (rel := RelationshipsSegment.capture(owner, relations)) is not None:
            segments.append(rel)
        if (act := PartnerActivitySegment.capture(owner, relations, self.goals)) is not None:
            segments.append(act)
        for segment in segments:
            context = context.append(segment)
        return context
