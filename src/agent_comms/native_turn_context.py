"""Native SDK input observations, decoded once by the existing Pi boundary."""

from dataclasses import dataclass, field, replace
from functools import partial

from .pi_payloads import PiResponseData
from .native_session_reopen import NativeSessionIdentity
from .turn_context import (
    ContextManifest, ContextSegment, ContextSourceText, MeasuredNativeSegment, PreviewProvenance,
    Provenance, SegmentManifest, TurnContext,
)


@dataclass(frozen=True)
class NativeContextManifestData(PiResponseData):
    strict_fields = True
    counter: str
    segments: tuple[SegmentManifest, ...]
    request_id: str | None = field(default=None, metadata={"wire_name": "requestId", "wire_omit_default": True})

    def for_turn(self, thread, turn):
        return ContextManifest(thread, turn, self.segments, self.counter, request_id=self.request_id)

    async def record(self, log, thread, lease) -> None:
        """Publish the original SDK observation under its leased owner turn."""
        from .coordinator import Coordination
        from .thread_identity import TurnId
        from .turn_context import RecordedContextTurn

        turn = RecordedContextTurn(TurnId(lease.turn_id), lease.identity)
        await Coordination.run_worker(
            partial(log.record_context, self.for_turn(thread.incarnation, turn))
        )


@dataclass(frozen=True)
class NativeContextData(PiResponseData):
    strict_fields = True
    counter: str
    identity: NativeSessionIdentity
    segments: tuple[MeasuredNativeSegment, ...]
    contributors: tuple[ContextSegment, ...] = ()

    def with_current_contributors(self, comms, owner):
        self.require_session_file(owner.require_saved_session())
        context = TurnContext.for_inspection(comms, owner)
        return replace(self, contributors=context.segments)

    @property
    def inspection_segments(self) -> tuple[ContextSegment, ...]:
        """The RPC's original ordering for reference selection, not a store."""
        return (*self.segments, *self.contributors)

    def observation(self) -> PreviewProvenance:
        """Return the SDK's original observation, without deriving its digest."""
        originals = {observation for segment in self.segments
                     for source in segment.provenance
                     for observation in source.preview_observations()}
        if len(originals) != 1:
            raise ValueError("Native context has no unambiguous current preview observation")
        (original,) = originals
        self.identity.require_same_session(original.identity)
        return original

    def public_source_text(self, comms, observation: PreviewProvenance,
                           segment: int, source: Provenance) -> ContextSourceText:
        if observation != self.observation():
            raise ValueError("Current native preview changed since the selected observation")
        if not 0 <= segment < len(self.inspection_segments):
            raise ValueError("Source has no selected context segment")
        selected = self.inspection_segments[segment].require_source(source)
        return ContextSourceText(selected.public_description(), selected.public_text(comms))

    def contributor_context(self, owner, turn):
        return TurnContext(owner.incarnation, turn, self.contributors)

    def for_turn(self, owner, turn):
        return TurnContext(owner.incarnation, turn, self.segments)

    def require_session_file(self, session_file):
        if self.identity.session_file != session_file:
            raise ValueError("Context inspection belongs to a different selected native session")
        return self
