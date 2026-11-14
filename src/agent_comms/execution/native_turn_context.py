"""Native SDK input observations, decoded once by the existing Pi boundary."""

from dataclasses import dataclass, field
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
    SentWhole,
    UnobservedDispatchRequest,
)
from agent_comms.nominal import field_codec
from agent_comms.pi_payloads import PiResponseData, StrictFields
from agent_comms.registry.thread_identity import OfferedTools


@dataclass(eq=False)
class PublishedContext:
    """One Pi process's published context, as Core holds it: the text of each value the
    process sent, which it names by hash from then on, what it sent of each segment of its
    last observation, and the request recorded from that observation, which its next one
    follows. It lives and dies with the process."""

    texts: dict[tuple[type[ContextSegment], str], tuple[str, ...]] = field(default_factory=dict)
    sent: dict[tuple[int, type[ContextSegment]], SentWhole] = field(default_factory=dict)
    """What this reader holds of each segment of the process's last observation, by its
    position."""
    prior: ContextPrior = UNREAD_CONTEXT

    def observed(self, context: NativeContextManifestData) -> ContextObserved:
        """The process's next observation, whole. Every observation passes here whether or
        not its turn records it, so a text or an entry is held from the moment its process
        sent it."""
        captured = context.captured()
        held = {}
        for position, (sent, manifest) in enumerate(zip(context.segments, captured, strict=True)):
            kind = manifest.kind
            held[position, kind] = self.sent.get((position, kind), kind.sending()).then(
                sent.after, manifest)
        self.sent = held
        return ContextObserved(context, self, tuple(
            manifest.kind.sent_once(self.texts, state.whole(manifest))
            for state, manifest in zip(held.values(), captured, strict=True)))


@dataclass(frozen=True)
class ContextObserved(AgentEvent):
    """Text-free SDK input observation; no transcript or status publication."""

    context: NativeContextManifestData
    published: PublishedContext
    segments: tuple[SegmentManifest, ...]
    """The observed segments, each with the text its process sent for it."""

    async def record(self, contexts, thread, lease) -> ContextManifest:
        """Publish the original SDK observation under its leased owner turn, after the
        request its process recorded last, which this one becomes."""
        manifest = self.context.manifest(
            thread.incarnation, RecordedContextTurn(lease.turn_id, lease.identity), self.segments)
        self.published.prior = await Coordination.run_worker(
            partial(contexts.record_after, self.published.prior, manifest))
        return manifest


@dataclass(frozen=True)
class SentSegment:
    """A segment as its Pi process sends it: its manifest, less the leading entries the
    process sent at this position in its previous observation, which ``after`` names by
    their mark (``SentWhole``)."""

    manifest: SegmentManifest
    after: Annotated[str, field_codec.WireOmitDefault()] = ""


@dataclass(frozen=True)
class NativeContextManifestData(StrictFields, PiResponseData):
    counter: str
    segments: tuple[SentSegment, ...]
    offered_tools: OfferedTools
    request_id: Annotated[
        DispatchRequest, field_codec.WireName("requestId"), field_codec.WireOmitDefault()
    ] = field(default_factory=UnobservedDispatchRequest)
    values: tuple[MeasuredNativeSegment, ...] = ()

    def captured(self) -> tuple[SegmentManifest, ...]:
        """Each segment as sent, with the public text of the values sent with it. A value
        belongs to what its observation sends: an entry sent before took its text then."""
        segments = tuple(sent.manifest for sent in self.segments)
        if any(not any(segment.contains_value(value) for segment in segments)
               for value in self.values):
            raise ValueError("Captured SDK value is outside the original manifest")
        return tuple(segment.capture_public(self.values) for segment in segments)

    def manifest(self, thread, turn, segments: tuple[SegmentManifest, ...]) -> ContextManifest:
        return ContextManifest(thread=thread, turn=turn, segments=segments,
                               counter=self.counter, request_id=self.request_id)
