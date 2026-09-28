"""Composition root: state and behavior live on their explicit owners."""

from __future__ import annotations

import os
from pathlib import Path

from .agent_activity import AgentActivity
from .channel_management import ChannelManagement
from .collaboration_ledger import CollaborationLedger
from .goal_management import Goals
from .history_views import HistoryViews
from .message_bus import MessageBus
from .messaging import Messaging
from .owner_lifecycle import OwnerLifecycle
from .registration import Registration
from .relationships import ThreadRelationships
from .thread_management import ThreadManagement
from .transcripts import Transcripts


class Comms:
    def __init__(
        self, root: Path, *, private_initial_writes: bool = True, private_claim_writes: bool = True
    ) -> None:
        self.root = Path(root).expanduser()
        if private_initial_writes or private_claim_writes:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._wire_lock_path = self.root / "wire"
        self.registry = Registration(self.root / "registry.json")
        self.bus = MessageBus(
            self.root / "bus.jsonl",
            self.registry,
            private_initial_writes=private_initial_writes,
            private_claim_writes=private_claim_writes,
        )
        self.bus.reads.migrate()
        self.channels = ChannelManagement(self.root, self.registry, self.bus)
        self.messaging = Messaging(self.root, self.registry, self.bus)
        self.agents = AgentActivity(self.root, self.registry)
        self.ledger = CollaborationLedger(self.root / CollaborationLedger.filename, self.registry)
        self.owners = OwnerLifecycle(self.root, self.registry, self.bus)
        self.goals = Goals(self.root, self.registry, self.bus)
        self.transcripts = Transcripts(self.root, self.registry, self.bus, self.messaging)
        self.threads = ThreadManagement(
            self.root, self.registry, self.bus, self.channels, self.agents, self.owners, self.ledger
        )
        self.views = HistoryViews(
            self.root,
            self.registry,
            self.bus,
            self.channels,
            self.agents,
            self.transcripts,
            self.messaging,
            self.goals,
            self.ledger,
        )
        self.relationships = ThreadRelationships(self.root, self.registry, self.bus, self.views)


def wire(root: Path | str | None = None) -> Comms:
    """Build a Comms wire from an explicit root or the active default route."""
    active_route = None
    if root is None:
        if "AGENT_COMMS_ROOT" in os.environ:
            root = os.environ["AGENT_COMMS_ROOT"]
        else:
            from .active_route import read_active_route

            active_route = read_active_route()
            root = active_route.root if active_route is not None else "~/.agent-comms"
    comms = Comms(Path(root).expanduser())
    if active_route is not None:
        comms.owners.pin_private_nk_launch(
            active_route.root, active_route.wire_root_id, active_route.native_package
        )
    return comms
