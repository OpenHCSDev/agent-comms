"""The producer's item identity within its native turn."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from .field_codec import Id128, NominalSpelling, NominalText

if TYPE_CHECKING:
    from .routing import Routing
    from .transcript_routes import _SessionRoutes


class NativeTurnId(NominalText):
    """A harness's own turn key: a Codex turn id, a Claude prompt id, an OpenCode message id
    or a Pi input id. Never one of our Id128 kinds; only equality is defined.
    """


class NativeTurnIdText(NominalSpelling[NativeTurnId]):
    spelling = re.compile(r"\S{1,4096}")


class FreshNativeId(NominalText):
    """An opaque harness key Core may mint, with the harness's free text spelling."""

    @classmethod
    def fresh(cls) -> Self:
        return cls(Id128.mint())

    def routing_in(self, routes: _SessionRoutes) -> Routing:
        return routes[self]


class FreshNativeIdText(NominalSpelling[FreshNativeId]):
    spelling = NativeTurnIdText.spelling


class PiRequestKey(FreshNativeId):
    """A Pi command's request correlation key."""


class NativeItemId(FreshNativeId):
    """One item within a harness's turn."""


class UsageChargeId(NominalText):
    """A billing entry within a goal attempt."""


class UsageChargeIdText(NominalSpelling[UsageChargeId]):
    spelling = NativeTurnIdText.spelling


class ProviderResponseId(UsageChargeId):
    """The provider's original response identity, decoded from Pi."""


class CompactionChargeId(UsageChargeId, FreshNativeId):
    """A locally minted billing entry for a compaction without a response id."""


@dataclass(frozen=True)
class TurnItem:
    turn_id: NativeTurnId
    item_id: NativeItemId

    @property
    def key(self) -> TurnItem:
        return TurnItem(self.turn_id, self.item_id)
