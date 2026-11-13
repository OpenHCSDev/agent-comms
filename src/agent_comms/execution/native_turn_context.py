"""Native SDK input observations, decoded once by the existing Pi boundary."""

from dataclasses import dataclass, field, replace
from functools import partial
from typing import Annotated

from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.agent_event import AgentEvent
from agent_comms.execution.context_prior import UNREAD_CONTEXT, ContextPrior
from agent_comms.execution.turn_context import (
    ContextManifest,
    ContextSegment,
    DispatchRequest,
    MeasuredNativeSegment,
    RecordedContextTurn,
    SegmentManifest,
    UnobservedDispatchRequest,
)
from agent_comms.nominal import field_codec
from agent_comms.pi_payloads import PiResponseData, StrictFields
from agent_comms.registry.thread_identity import OfferedTools


@dataclass(eq=False)
class PublishedContext:
    """One Pi process's published context, as Core holds it: the text of each value the
    process sent, which it names by hash from then on, and the request recorded from its
    last observation, which its next one follows. It lives and dies with the process."""

    texts: dict[tuple[type[ContextSegment], str], tuple[str, ...]] = field(default_factory=dict)
    prior: ContextPrior = UNREAD_CONTEXT

    async def record(
        self, context: NativeContextManifestData, contexts, thread, lease,
    ) -> ContextManifest:
        """Record the process's next observation after its last, and return it whole."""
        observed = context.for_turn(
            thread.incarnation, RecordedContextTurn(lease.turn_id, lease.identity))
        manifest = replace(observed, segments=tuple(
            segment.kind.sent_once(self.texts, segment) for segment in observed.segments))
        self.prior = await Coordination.run_worker(
            partial(contexts.record_after, self.prior, manifest))
        return manifest


@dataclass(frozen=True)
class ContextObserved(AgentEvent):
    """Text-free SDK input observation; no transcript or status publication."""

    context: NativeContextManifestData
    published: PublishedContext

    async def record(self, contexts, thread, lease) -> ContextManifest:
        """Publish the original SDK observation under its leased owner turn, after the
        request its process recorded last."""
        return await self.published.record(self.context, contexts, thread, lease)


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
