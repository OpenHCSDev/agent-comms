"""Native SDK input observations, decoded once by the existing Pi boundary."""

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Annotated

from agent_comms import field_codec

from .native_session_reopen import NativeSessionIdentity
from .pi_payloads import PiResponseData, StrictFields
from .tool_result import DeclaredReply
from .thread_identity import OfferedTools, UnreportedTools
from .turn_context import (
    ContextManifest,
    ContextSourceText,
    DispatchRequest,
    MeasuredNativeSegment,
    PreviewProvenance,
    Provenance,
    SegmentManifest,
    TurnContext,
    UnobservedDispatchRequest,
)

if TYPE_CHECKING:
    from .context_store import CapturedContext


@dataclass(frozen=True)
class NativeContextManifestData(StrictFields, PiResponseData):
    counter: str
    segments: tuple[SegmentManifest, ...]
    request_id: Annotated[
        DispatchRequest, field_codec.WireName("requestId"), field_codec.WireOmitDefault()
    ] = field(default_factory=UnobservedDispatchRequest)
    values: tuple[MeasuredNativeSegment, ...] = ()
    offered_tools: OfferedTools = UnreportedTools('unreported')

    def for_turn(self, thread, turn):
        if any(not any(segment.contains_value(value) for segment in self.segments)
               for value in self.values):
            raise ValueError("Captured SDK value is outside the original manifest")
        return ContextManifest(thread, turn, tuple(segment.capture_public(self.values)
            for segment in self.segments), self.counter, request_id=self.request_id)

    async def record(self, contexts, prior, thread, lease) -> CapturedContext:
        """Publish the original SDK observation under its leased owner turn, after the
        prior this turn holds; the result is the turn's next prior."""
        from .coordinator import Coordination
        from .thread_identity import TurnId
        from .turn_context import RecordedContextTurn

        turn = RecordedContextTurn(TurnId(lease.turn_id), lease.identity)
        manifest = self.for_turn(thread.incarnation, turn)
        return await Coordination.run_worker(
            partial(contexts.record_after, prior, manifest)
        )


@dataclass(frozen=True)
class NativeContextData(StrictFields, PiResponseData, DeclaredReply):
    counter: str
    identity: NativeSessionIdentity
    segments: tuple[MeasuredNativeSegment, ...]

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
        if not 0 <= segment < len(self.segments):
            raise ValueError("Source has no selected context segment")
        selected = self.segments[segment].require_source(source)
        return ContextSourceText(selected.public_description(), selected.public_text(comms))

    def recorded_public_text(self, expected: SegmentManifest) -> str:
        self.identity.require_same_session(expected.sources.native_identity())
        originals = {}
        for original in expected.original_values():
            originals.setdefault(original.sha256, []).append(original)
        values = {}
        for segment in self.segments:
            if not any(segment.matches_recorded(original)
                       for original in originals.get(segment.sha256, ())):
                raise ValueError("Recorded SDK source is outside the selected original value")
            values.setdefault(segment.sha256, []).append(segment)
        return expected.recorded_public_text(values)

    def for_turn(self, owner, turn):
        return TurnContext(owner.incarnation, turn, self.segments)

    def require_session_file(self, session_file):
        if self.identity.session_file != session_file:
            raise ValueError("Context inspection belongs to a different selected native session")
        return self
