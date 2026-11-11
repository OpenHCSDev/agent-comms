from __future__ import annotations

import json
from dataclasses import dataclass

from agent_comms.messaging.read_basis import ChannelDisplayScope, ReadCursor, UnreadQuery
from agent_comms.messaging.unread_query_source import UnreadQuerySource
from agent_comms.registry.registry_provenance import RegistryProvenance


@dataclass(frozen=True, slots=True)
class DisplayUnreadSelection(UnreadQuerySource):
    """The complete captured inputs that determine an unread count."""

    seen: ReadCursor
    scopes: tuple[ChannelDisplayScope, ...]
    viewer_names: tuple[str, ...]
    registry: RegistryProvenance

    def queries(self) -> tuple[UnreadQuery, ...]:
        """Each scope's count resolved against this registry and cursor."""
        names = json.dumps(self.viewer_names)
        return tuple(
            UnreadQuery(
                scope.channel,
                ReadCursor.rows(self.registry, {"viewer": self.seen.counted(scope, self.registry)}),
                self.seen.lower_bound(scope, self.registry),
                *scope.index_predicate,
                names,
            )
            for scope in self.scopes
        )
