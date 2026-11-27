"""Shared read access to a bus root's goal waits and attempt ledger.

Both GoalSnapshotReader (read-only CLI projections) and GoalRecords (full goal
management) inherit this so the root, agent activity, wait store, and attempt
store are declared once without coupling GoalRecords to goal_presentation.
"""

from __future__ import annotations

from pathlib import Path

from agent_comms.planning.goal_attempts import GoalAttemptFile, GoalAttemptStore
from agent_comms.planning.goal_waits import GoalWaits
from agent_comms.registry.agent_activity import AgentActivity


class GoalStoreAccess:
    """Shared root, activity, wait store, and lazy attempt ledger handle."""

    def __init__(self, root: Path, agents: AgentActivity) -> None:
        self.root = root
        self.agents = agents
        self.waits = GoalWaits(root)
        self.goal_store: GoalAttemptFile = GoalAttemptFile(GoalAttemptStore.path_in(
            GoalAttemptStore.directory_in(root)))

    def open_goal_store(self) -> GoalAttemptStore:
        store = self.goal_store.open()
        self.goal_store = store
        return store
