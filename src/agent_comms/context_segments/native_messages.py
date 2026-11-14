"""Native provider-message segments in the declared context family."""

from __future__ import annotations

from dataclasses import (
    dataclass,
    replace,
)
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.execution.annotation_disclosure import PublicInstructionAnnotations
from agent_comms.pi_payloads import PiMessage
from agent_comms.execution.turn_context import ContextEntryManifest, ProviderContext
from agent_comms.execution.turn_context import ContextEntryReference
from agent_comms.execution.turn_context import ContextSpan
from agent_comms.execution.turn_context import ContributionCoordinates
from agent_comms.execution.turn_context import MeasuredNativeSegment
from agent_comms.execution.turn_context import RecordedSegmentManifest
from agent_comms.execution.turn_context import SegmentManifest
from agent_comms.execution.turn_context import SegmentRecording
from agent_comms.execution.turn_context import SentEntries
from agent_comms.execution.turn_context import UnrecordedSegment


@dataclass(frozen=True, kw_only=True)
class NativeMessages(MeasuredNativeSegment, declared_name=None):
    # These are the SDK's exact *provider* messages, including opaque extension
    # and provider fields. Core does not decode them as its event/message subset.
    messages: tuple[dict[str, JsonValue], ...]

    @classmethod
    def recorded_parts(cls, original: SegmentManifest) -> tuple[SegmentManifest, ...]:
        # SDK capture emits one same-kind child per provider message. A single
        # message's logical input-range annotations have other declared kinds.
        if all(part.kind is cls for part in original.contributors):
            return original.contributors
        return ()

    def provider_value(self):
        return list(self.messages)

    def public_text(self) -> str:
        return "\n".join(PiMessage.from_wire(message).text for message in self.messages)

    def source_ranges(self) -> tuple[ContributionCoordinates, ...]:
        offset = 0
        ranges = []
        parts = self.recorded_parts(self.measured_manifest())
        for index, message in enumerate(self.messages):
            text = PiMessage.from_wire(message).text
            provenance = parts[index].provenance if parts else self.provenance
            ranges.append(ContributionCoordinates.capture(type(self), provenance, offset, text))
            offset += len(text.encode("utf-8")) + 1
        return tuple(ranges)

    def render_into(self, prompt_parts, provider: ProviderContext) -> ProviderContext:
        provider.messages.extend(self.messages)
        return provider


class NativeAnnotations(NativeMessages, declared_name=None):
    """A transcript span keeps the disclosure policy of its original native message."""

    def disclosure_for(self, span: ContextSpan):
        for source, message in zip(self.source_ranges(), self.messages, strict=True):
            if source.contains(span.coordinates):
                return PiMessage.from_wire(message).disclosure_for(span)
        raise ValueError("Annotation names no original native message")


@dataclass(frozen=True, kw_only=True)
class TranscriptSegment(NativeAnnotations):
    keyframe_interval: ClassVar[int] = 32

    sending = SentEntries

    @classmethod
    def record_manifest(
        cls, original: SegmentManifest, previous: SegmentRecording,
        *, recording_turn: int = 1,
    ) -> tuple[RecordedSegmentManifest, tuple[ContextEntryManifest, ...]]:
        if not original.contributors:
            return super().record_manifest(original, previous, recording_turn=recording_turn)
        provenance = tuple(source for source in original.provenance
                           for source in source.turn_recording())
        header = replace(original, provenance=provenance, contributors=(), captured_text=())
        references = tuple(ContextEntryReference.from_sources(child.sources)
                           for child in original.contributors)
        # Asked of the request really recorded before this one: a keyframe turn records as
        # if nothing came before, and its entries are in the store all the same.
        held = previous.held_entries(references)
        if not recording_turn % cls.keyframe_interval:
            previous = UnrecordedSegment()
        recorded = previous.record_entries(header, references)
        entries = tuple(ContextEntryManifest(child) for child in original.contributors[held:])
        return recorded, entries


@dataclass(frozen=True, kw_only=True)
class TransformedInputSegment(NativeAnnotations):
    """SDK values without original-entry correspondence carry no user-entry proof."""


@dataclass(frozen=True, kw_only=True)
class CompactionSummarySegment(PublicInstructionAnnotations, NativeMessages):
    pass


@dataclass(frozen=True, kw_only=True)
class InjectionMessageSegment(NativeAnnotations):
    pass
