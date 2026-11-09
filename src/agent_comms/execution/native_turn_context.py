"""Native SDK input observations, decoded once by the existing Pi boundary."""

from dataclasses import dataclass, field
from functools import partial
from typing import Annotated, TYPE_CHECKING

from .turn_context import ContextManifest, DispatchRequest, MeasuredNativeSegment, RecordedContextTurn, SegmentManifest, UnobservedDispatchRequest
from ..agent_events import AgentEvent
from ..coordination.coordinator import Coordination
from ..nominal import field_codec
from ..pi_payloads import PiResponseData, StrictFields
from ..registry.thread_identity import OfferedTools

if TYPE_CHECKING:
    from ..context_store import CapturedContext


@dataclass(frozen=True)
class ContextObserved(AgentEvent):
    """Text-free SDK input observation; no transcript or status publication."""

    context: NativeContextManifestData


@dataclass(frozen=True)
class NativeContextManifestData(StrictFields, PiResponseData):
    counter: str
    segments: tuple[SegmentManifest, ...]
    offered_tools: OfferedTools
    request_id: Annotated[
        DispatchRequest, field_codec.WireName("requestId"), field_codec.WireOmitDefault()
    ] = field(default_factory=UnobservedDispatchRequest)
    values: tuple[MeasuredNativeSegment, ...] = ()

    def for_turn(self, thread, turn):
        if any(not any(segment.contains_value(value) for segment in self.segments)
               for value in self.values):
            raise ValueError("Captured SDK value is outside the original manifest")
        return ContextManifest(thread, turn, tuple(segment.capture_public(self.values)
            for segment in self.segments), self.counter, request_id=self.request_id)

    async def record(self, contexts, prior, thread, lease) -> CapturedContext:
        """Publish the original SDK observation under its leased owner turn, after the
        prior this turn holds; the result is the turn's next prior."""
        turn = RecordedContextTurn(lease.turn_id, lease.identity)
        manifest = self.for_turn(thread.incarnation, turn)
        return await Coordination.run_worker(
            partial(contexts.record_after, prior, manifest)
        )
