"""The independent evidence scopes one notification presentation needs."""

from agent_comms.messaging.human_read_cut import HumanReadCut
from agent_comms.notification_assignment import AssignmentSource


class NotificationSource(AssignmentSource, HumanReadCut):
    """Share assignment and human-display reads across the same short-lived pass."""
