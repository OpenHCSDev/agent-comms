"""The producer's item identity within its native turn."""

from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from .errors import RelationViolationError
from .field_codec import Id128, NominalSpelling, NominalText, NullMember, RecordMember, SubclassUnion

if TYPE_CHECKING:
    from .channel_targets import NamedTarget
    from .coordination_session import NativeCapture
    from .external_transcript import ExternalTranscript
    from .message_reference import MessageReference
    from .routing import Routing
    from .transcript_routes import _SessionRoutes


class NativeTurnId(NominalText):
    """A harness's own turn key: a Codex turn id, a Claude opening record UUID, an OpenCode message id
    or a Pi input id. Never one of our Id128 kinds; only equality is defined.
    """


class NativeTurnIdText(NominalSpelling[NativeTurnId]):
    spelling = re.compile(r"\S{1,4096}")


class SourceTurn(SubclassUnion, affix="SourceTurn"):
    """A source-named turn, never a guess from the newest transcript row."""

    def external_answers(
        self, capture: NativeCapture, transcript: ExternalTranscript, route: NamedTarget,
    ) -> tuple[MessageReference, ...]:
        """An absent source opens neither the registry nor the external transcript."""
        return ()

    @abstractmethod
    def require_id(self) -> NativeTurnId: ...


@dataclass(frozen=True)
class NoSourceTurn(NullMember, SourceTurn):
    """No external turn was supplied; no external answer links may be inferred."""

    def require_id(self) -> NativeTurnId:
        raise RelationViolationError("Confirmed input has no source turn")


@dataclass(frozen=True)
class NamedSourceTurn(RecordMember, SourceTurn):
    id: NativeTurnId

    def require_id(self) -> NativeTurnId:
        return self.id

    def external_answers(
        self, capture: NativeCapture, transcript: ExternalTranscript, route: NamedTarget,
    ) -> tuple[MessageReference, ...]:
        with capture as boundary, boundary.reading() as source:
            return source.answering(transcript.opening_for(self.id), route, boundary.registry)


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
