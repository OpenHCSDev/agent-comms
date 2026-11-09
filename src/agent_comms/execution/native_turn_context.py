"""Native SDK input observations, decoded once by the existing Pi boundary."""

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Annotated

from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.agent_event import AgentEvent
from agent_comms.execution.turn_context import (
    ContextManifest,
    DispatchRequest,
    MeasuredNativeSegment,
    RecordedContextTurn,
    SegmentManifest,
    UnobservedDispatchRequest,
)
from agent_comms.nominal import field_codec
from agent_comms.pi_payloads import PiResponseData, StrictFields
from agent_comms.registry.thread_identity import OfferedTools

if TYPE_CHECKING:
    from agent_comms.context_store import CapturedContext


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
        return ContextManifest(thread=thread, turn=turn, segments=tuple(segment.capture_public(self.values)
            for segment in self.segments), counter=self.counter, request_id=self.request_id)

    async def record(self, contexts, prior, thread, lease) -> CapturedContext:
        """Publish the original SDK observation under its leased owner turn, after the
        prior this turn holds; the result is the turn's next prior."""
        turn = RecordedContextTurn(lease.turn_id, lease.identity)
        manifest = self.for_turn(thread.incarnation, turn)
        return await Coordination.run_worker(
            partial(contexts.record_after, prior, manifest)
        )
