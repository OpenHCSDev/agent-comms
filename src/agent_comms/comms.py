"""Composition root: state and behavior live on their explicit owners."""

from __future__ import annotations

from pathlib import Path
from .active_route import CommsRoute

from .agent_activity import AgentActivity
from .channel_management import ChannelManagement
from .catalog_store import ChannelCatalog
from .collaboration_ledger import CollaborationLedger
from .goal_management import Goals
from .history_views import HistoryViews
from .message_bus import MessageBus
from .messaging import Messaging
from .owner_lifecycle import OwnerLifecycle
from .native_settings import NativeThreadDefaults
from .owner_launch import LaunchRuntime
from .relationships import ThreadRelationships
from .thread_management import ThreadManagement
from .thread_configuration import ThreadConfiguration
from .transcripts import Transcripts


class Comms:
    def __init__(
        self, route: CommsRoute, *, private_initial_writes: bool = True, private_claim_writes: bool = True,
        runtime: LaunchRuntime,
    ) -> None:
        self.route = route
        self.runtime = runtime
        if private_initial_writes or private_claim_writes:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._wire_lock_path = self.root / "wire"
        self.bus = MessageBus.for_root(
            self.root,
            private_initial_writes=private_initial_writes,
            private_claim_writes=private_claim_writes,
        )
        self.registry = self.bus.registry
        self.messaging = Messaging(self.root, self.registry, self.bus)
        self.agents = AgentActivity(self.root, self.registry)
        self.ledger = CollaborationLedger(self.root / CollaborationLedger.filename, self.registry)
        defaults = NativeThreadDefaults.from_runtime(runtime)
        self.owners = OwnerLifecycle(self.root, self.registry, self.bus, native=defaults)
        self.goals = Goals(self.root, self.registry, self.bus)
        self.transcripts = Transcripts(self.root, self.registry, self.bus, self.messaging)
        self.threads = ThreadManagement(
            self.root, self.registry, self.bus,
            ChannelCatalog(self.root / ChannelCatalog.filename), self.agents, self.owners, self.ledger
        )
        self.configuration = ThreadConfiguration(self.root, self.registry, self.threads, self.agents)
        self.channels = ChannelManagement(self.root, self.registry, self.bus, self.threads)
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
        self.route.bind_owners(self.owners)

    @property
    def root(self) -> Path:
        return self.route.root
