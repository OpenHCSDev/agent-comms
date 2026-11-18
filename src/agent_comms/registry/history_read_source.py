"""The existing relationship and human-selection scopes a history reader needs."""

from agent_comms.execution.input_ingress import InputIngressContext
from agent_comms.registry.relationship_source import RelationshipSource
from agent_comms.registry.threads import Thread


class HistoryReadSource(RelationshipSource, InputIngressContext[Thread]):
    """Read relationships and select a human without depending on the views facade."""
