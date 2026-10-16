"""Native SDK input observations, decoded once by the existing Pi boundary."""

from dataclasses import dataclass, field
from functools import partial

from .pi_payloads import PiResponseData
from .native_session_reopen import NativeSessionIdentity
from .turn_context import (
    ContextManifest, ContextSourceText, MeasuredNativeSegment, PreviewProvenance,
    Provenance, SegmentManifest, TurnContext,
)


@dataclass(frozen=True)
class NativeContextManifestData(PiResponseData):
    strict_fields = True
    counter: str
    segments: tuple[SegmentManifest, ...]
    request_id: str | None = field(default=None, metadata={"wire_name": "requestId", "wire_omit_default": True})
    values: tuple[MeasuredNativeSegment, ...] = ()

    def for_turn(self, thread, turn):
        if any(not any(segment.contains_value(value) for segment in self.segments)
               for value in self.values):
            raise ValueError("Captured SDK value is outside the original manifest")
        return ContextManifest(thread, turn, tuple(segment.capture_public(self.values)
            for segment in self.segments), self.counter, request_id=self.request_id)

    async def record(self, log, thread, lease) -> ContextManifest:
        """Publish the original SDK observation under its leased owner turn."""
        from .coordinator import Coordination
        from .thread_identity import TurnId
        from .turn_context import RecordedContextTurn

        turn = RecordedContextTurn(TurnId(lease.turn_id), lease.identity)
        manifest = self.for_turn(thread.incarnation, turn)
        await Coordination.run_worker(
            partial(log.record_context, manifest)
        )
        return manifest


@dataclass(frozen=True)
class NativeContextData(PiResponseData):
    strict_fields = True
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
        self.identity.require_same_session(expected.native_identity())
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
