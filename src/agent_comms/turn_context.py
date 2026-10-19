"""Original turn contributors and their observations, never input authority."""

from __future__ import annotations

import hashlib
import json
import re
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from .cli_result import CliResult
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .message_reference import MessageReference
from .native_session_reopen import NativeSessionIdentity
from .peer_relevance import AlwaysListed, GoalShown, PeerRelevance
from .thread_identity import ThreadIncarnation, TurnId, TurnIdentity
from .tool_result import DeclaredReply

if TYPE_CHECKING:
    from .input_attempt import StoredInput
    from .presentation import ThreadView
    from .private_bus_checkpoint import CapturedWireSource
    from .relationships import PeerRelation
    from .threads import Thread


@dataclass(frozen=True)
class Provenance(DeclaredFamily, affix="Provenance"):
    """A source coordinate, not permission to execute or replay it."""

    @abstractmethod
    def public_description(self) -> str: ...

    def public_text(self, comms) -> str:
        """Read an authenticated source, never reconstruct request wording.

        The inspection owner checks membership before calling this method.
        Coordinates without original text evidence remain unavailable.
        """
        raise ValueError(f"Original text is not retained by {self.public_description()}")

    def preview_observations(self) -> tuple[PreviewProvenance, ...]:
        return ()

    def native_identities(self) -> tuple[NativeSessionIdentity, ...]:
        return ()

    def journal_entries(self, identity: NativeSessionIdentity) -> tuple[str, ...]:
        return ()

    def turn_recording(self) -> tuple[Provenance, ...]:
        return (self,)

    def entry_membership(self) -> tuple[Provenance, ...]:
        return ()

    def require_human_input(self):
        from .errors import RelationViolationError

        raise RelationViolationError("Source is not an original human input")


@dataclass(frozen=True)
class FileProvenance(Provenance):
    path: str
    sha256: str

    def public_description(self) -> str:
        return f"File {self.path} · SHA256 {self.sha256}"

    def public_text(self, comms) -> str:
        raw = Path(self.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("Original file bytes are unavailable: the source has changed")
        return raw.decode("utf-8")


@dataclass(frozen=True)
class CodexRolloutProvenance(Provenance):
    """Authenticate historical role-record bytes; never current instructions."""

    path: str
    offset: int
    length: int
    sha256: str
    instruction: int

    def public_description(self) -> str:
        return (f"Historical Codex instruction · {self.path} · record bytes "
                f"{self.offset}:{self.offset + self.length} · item {self.instruction} "
                "· not current instructions")

    def public_text(self, comms) -> str:
        from .cli_bridges.codex_rollout import ROLLOUT_RECORD

        if min(self.offset, self.length, self.instruction) < 0:
            raise ValueError("Historical Codex source coordinates must be nonnegative")
        with Path(self.path).open("rb") as source:
            source.seek(self.offset)
            raw = source.read(self.length)
        if len(raw) != self.length or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("Historical Codex source record changed or is unavailable")
        try:
            instruction = ROLLOUT_RECORD.validate_json(raw).instructions()[self.instruction]
        except IndexError as error:
            raise ValueError("Historical instruction is outside its original record") from error
        return instruction.text


@dataclass(frozen=True)
class OwnerProvenance(Provenance):
    owner: ThreadIncarnation
    revision: str

    def public_description(self) -> str:
        return f"Owner {self.owner.name} · born {self.owner.created_at} · revision {self.revision}"


@dataclass(frozen=True)
class WireProvenance(Provenance):
    source: MessageReference

    def public_description(self) -> str:
        return f"Wire message {self.source.seq} · {self.source.message_id}"

    def public_text(self, comms) -> str:
        (original,) = comms.bus.log.messages_for_references((self.source,))
        return original.body


@dataclass(frozen=True)
class JournalProvenance(Provenance):
    path: str
    entries: tuple[str, ...]

    def public_description(self) -> str:
        return f"Native journal {self.path} · entries {', '.join(self.entries)}"

    def journal_entries(self, identity: NativeSessionIdentity) -> tuple[str, ...]:
        identity.require_session(self.path)
        return self.entries

    def turn_recording(self) -> tuple[Provenance, ...]:
        return ()

    def entry_membership(self) -> tuple[Provenance, ...]:
        return (self,)


@dataclass(frozen=True)
class NativeProvenance(Provenance):
    identity: NativeSessionIdentity
    request_generation: int
    context_digest: str

    def public_description(self) -> str:
        return (f"Native request {self.identity.session_id} · generation {self.request_generation} "
                f"· context {self.context_digest}")

    def native_identities(self) -> tuple[NativeSessionIdentity, ...]:
        return (self.identity,)


@dataclass(frozen=True)
class PreviewProvenance(Provenance):
    identity: NativeSessionIdentity
    context_digest: str

    def public_description(self) -> str:
        return f"Current native preview {self.identity.session_id} · context {self.context_digest}"

    def preview_observations(self) -> tuple[PreviewProvenance, ...]:
        return (self,)

    def native_identities(self) -> tuple[NativeSessionIdentity, ...]:
        """SDK identity for checking a read, never permission to start input."""
        return (self.identity,)


@dataclass(frozen=True)
class ResourceProvenance(Provenance):
    path: str
    sha256: str
    representation: str

    def public_description(self) -> str:
        return f"Resource {self.path} · {self.representation} · SHA256 {self.sha256}"


@dataclass(frozen=True)
class UnattributedProvenance(Provenance):
    """Captured wording with no original assembly attribution."""

    def public_description(self) -> str:
        return "Unattributed captured text · original source was not observed"


@dataclass(frozen=True)
class ContextSourceText(DeclaredReply):
    """A public source-read result, not another context or input record."""

    description: str
    text: str


@dataclass(frozen=True)
class InstructionFile:
    """One immutable read used for both rendering and file provenance."""

    content: str
    source: FileProvenance

    @classmethod
    def read(cls, name: str) -> InstructionFile:
        path = Path(__file__).with_name("instructions") / name
        raw = path.read_bytes()
        return cls(raw.decode(), FileProvenance(str(path), hashlib.sha256(raw).hexdigest()))

    def render(self, values: dict[str, object]) -> str:
        return self.content.format_map(values)


@dataclass(frozen=True)
class SegmentManifest:
    kind: type[ContextSegment]
    provenance: tuple[Provenance, ...]
    sha256: str
    utf8_bytes: int
    tokens: int
    contributors: tuple[SegmentManifest, ...] = ()
    captured_text: tuple[str, ...] = field(default=(), metadata={"wire_omit_default": True})
    source_spans: tuple[ContributionCoordinates, ...] = field(
        default=(), metadata={"wire_omit_default": True})

    def __post_init__(self):
        if not self.provenance or min(self.utf8_bytes, self.tokens) < 0:
            raise ValueError("Invalid original context segment observation")
        if len(self.sha256) != 64 or not set(self.sha256) <= set("0123456789abcdef"):
            raise ValueError("Context segment requires its measured byte digest")

    def remember_received(self, child) -> None:
        self.kind.remember_received(child, self.provenance)
        for contributor in self.contributors:
            contributor.remember_received(child)

    def source_membership(self):
        yield from self.provenance
        for span in self.source_spans:
            yield from span.provenance
        for contributor in self.contributors:
            yield from contributor.source_membership()

    def public_description(self) -> str:
        return f"{self.kind.public_title()} · {self.tokens} estimated tokens"

    def contains_span(self, span: ContextSpan) -> bool:
        """An annotation retains its original source, not just matching prose."""
        return (span.segment_sha256 == self.sha256
                and span.coordinates.kind is self.kind
                and self.kind.contains_contribution(self, span.coordinates))

    def selected_contributor(self, positions: tuple[int, ...]) -> SegmentManifest:
        selected = self
        for position in positions:
            if not 0 <= position < len(selected.contributors):
                raise ValueError("Original request has no selected contributor")
            selected = selected.contributors[position]
        return selected

    def capture_public(self, values: tuple[MeasuredNativeSegment, ...]) -> SegmentManifest:
        """Project the original SDK publication; never capture a later preview."""
        matched = tuple(value for value in values if value.measured_manifest() == self)
        text = self.captured_text
        if matched:
            public = {value.public_text() for value in matched}
            if len(public) != 1:
                raise ValueError("Captured SDK public text is ambiguous")
            text = tuple(public)
        return replace(self, captured_text=text,
                       contributors=tuple(child.capture_public(values) for child in self.contributors))

    def contains_value(self, value: MeasuredNativeSegment) -> bool:
        return (value.measured_manifest() == self
                or any(child.contains_value(value) for child in self.contributors))

    def native_identity(self) -> NativeSessionIdentity:
        originals = {identity for source in self.provenance for identity in source.native_identities()}
        if len(originals) != 1:
            raise ValueError("Original segment has no unambiguous native source identity")
        (identity,) = originals
        return identity

    def journal_entries(self) -> tuple[str, ...]:
        identity = self.native_identity()
        entries = tuple(dict.fromkeys(entry for source in self.provenance
                                      for entry in source.journal_entries(identity)))
        if not entries:
            raise ValueError("Original segment has no recoverable journal coordinates")
        return entries

    async def public_text(self, read_reference) -> str:
        if self.public_text_recorded:
            return self.recorded_public_text({})
        return await read_reference(self)

    def recorded_parts(self) -> tuple[SegmentManifest, ...]:
        return self.kind.recorded_parts(self)

    def requested_parts(self) -> tuple[SegmentManifest, ...]:
        return tuple(part for part in self.recorded_parts() if not part.public_text_recorded)

    def recorded_public_text(self, values_by_digest) -> str:
        """Render the one acquired SDK result alongside original captures.

        The SDK reader owns complete source resolution. A returned whole value
        takes precedence over its annotation children; otherwise the same result
        contains all uncaptured message parts. No child opens another reader.
        """
        if self.captured_text:
            return "\n".join(self.captured_text)
        for value in values_by_digest.get(self.sha256, ()):
            if value.matches_recorded(self):
                return value.public_text()
        parts = self.recorded_parts()
        if parts:
            return "\n".join(child.recorded_public_text(values_by_digest)
                             for child in parts)
        raise ValueError("Recorded SDK source differs from the original measured bytes")

    def original_values(self):
        yield self
        for contributor in self.contributors:
            yield from contributor.original_values()

    @property
    def public_text_recorded(self) -> bool:
        if self.captured_text:
            return True
        parts = self.recorded_parts()
        return bool(parts) and all(part.public_text_recorded for part in parts)


@dataclass(frozen=True)
class ContextEntryReference:
    """One native journal entry whose manifest is recorded independently."""

    identity: NativeSessionIdentity
    entry_id: str

    @classmethod
    def from_manifest(cls, manifest: SegmentManifest) -> ContextEntryReference:
        entries = manifest.journal_entries()
        if len(entries) != 1:
            raise ValueError("A context entry manifest must name exactly one journal entry")
        return cls(manifest.native_identity(), entries[0])


@dataclass(frozen=True)
class ContextEntryManifest:
    """The one recorded manifest for a native transcript entry."""

    manifest: SegmentManifest

    @property
    def reference(self) -> ContextEntryReference:
        return ContextEntryReference.from_manifest(self.manifest)


@dataclass(frozen=True)
class RecordedSegmentManifest:
    """A turn segment with transcript contributors held by entry reference."""

    manifest: SegmentManifest
    entries: tuple[ContextEntryReference, ...] = ()
    previous: int | None = field(default=None, metadata={"wire_omit_default": True})
    inherited_entries: int = field(default=0, metadata={"wire_omit_default": True})

    def __post_init__(self) -> None:
        if self.entries and self.manifest.contributors:
            raise ValueError("Recorded segment cannot inline and reference contributors")
        if self.inherited_entries < 0 or (self.inherited_entries and self.previous is None):
            raise ValueError("Recorded transcript prefix requires its predecessor")

    def predecessors(self, offset: int) -> tuple[int, ...]:
        if self.previous is None:
            return ()
        if not 0 <= self.previous < offset:
            raise ValueError("Context predecessor is not an earlier record")
        return (self.previous,)

    def keyframe(
        self, original: SegmentManifest, offset: int | None,
    ) -> tuple[tuple[ContextEntryReference, ...], int | None]:
        if self.previous is None:
            count, source = len(self.entries), offset
        else:
            count, source = self.inherited_entries, self.previous
        return (tuple(ContextEntryReference.from_manifest(child)
                      for child in original.contributors[:count]), source)

    def record_text(
        self, original: SegmentManifest, previous_value: SegmentManifest, offset: int | None,
    ) -> RecordedSegmentManifest:
        if (original.sha256 == previous_value.sha256
                and original.captured_text and previous_value.captured_text):
            reference = offset if self.manifest.captured_text else self.previous
            if reference is None:
                raise ValueError("Recorded text predecessor is missing")
            return RecordedSegmentManifest(replace(original, captured_text=()), previous=reference)
        return RecordedSegmentManifest(original)

    def restore(self, entries: dict[ContextEntryReference, SegmentManifest],
                predecessors: dict[int, RecordedContextManifest] | None = None,
                position: int = 0) -> SegmentManifest:
        prior = (predecessors[self.previous].segments[position]
                 if self.previous is not None and predecessors is not None else None)
        previous = prior.restore(entries) if prior is not None else None
        original = self.manifest
        if self.previous is not None and previous is None:
            raise ValueError("Recorded segment predecessor is missing")
        if previous is not None and previous.kind is not original.kind:
            raise ValueError("Recorded segment predecessor has a different kind")
        if (previous is not None and previous.sha256 == original.sha256
                and not original.captured_text):
            original = replace(original, captured_text=previous.captured_text)
        if not self.entries and not self.inherited_entries:
            return original
        if self.inherited_entries and (
                previous is None or self.inherited_entries > len(previous.contributors)):
            raise ValueError("Recorded transcript prefix is missing")
        contributors = (previous.contributors[:self.inherited_entries] if previous is not None else ())
        contributors += tuple(entries[reference] for reference in self.entries)
        membership = tuple(
            source
            for contributor in contributors
            for source in contributor.provenance
            for source in source.entry_membership()
        )
        return replace(
            original,
            provenance=(*original.provenance, *membership),
            contributors=contributors,
        )


@dataclass(frozen=True, kw_only=True)
class ContextSegment(
    DeclaredFamily, affix="Segment", discovery_package="agent_comms.context_segments"
):
    provenance: tuple[Provenance, ...]

    def __post_init__(self) -> None:
        if not self.provenance:
            raise TypeError("A context contribution requires its original source")

    @abstractmethod
    def text(self) -> str: ...

    @classmethod
    def public_title(cls) -> str:
        return cls.declared_name.replace("_", " ").title()

    def public_description(self) -> str:
        return self.public_title()

    def needed_after(self, received: tuple[Provenance, ...]) -> bool:
        return True

    @classmethod
    def remember_received(cls, child, provenance):
        pass

    def contributor_manifests(self) -> tuple[SegmentManifest, ...]:
        return ()

    @classmethod
    def recorded_parts(cls, original: SegmentManifest) -> tuple[SegmentManifest, ...]:
        # Source annotations do not partition a complete provider value.
        return ()

    @classmethod
    def record_manifest(
        cls, original: SegmentManifest, previous: RecordedSegmentManifest | None = None,
        previous_value: SegmentManifest | None = None, previous_offset: int | None = None,
        *, recording_turn: int = 1,
    ) -> tuple[RecordedSegmentManifest, tuple[ContextEntryManifest, ...]]:
        recorded = RecordedSegmentManifest(original)
        if previous is not None and previous_value is not None:
            recorded = previous.record_text(original, previous_value, previous_offset)
        return recorded, ()

    def public_text(self) -> str:
        return self.text()

    def source_ranges(self) -> tuple[ContributionCoordinates, ...]:
        return (ContributionCoordinates.capture(
            type(self), self.provenance, 0, self.public_text()),)

    @classmethod
    def contains_contribution(cls, original: SegmentManifest, coordinates: ContributionCoordinates) -> bool:
        return all(source in original.source_membership() for source in coordinates.provenance)

    def public_spans(self) -> tuple[ContextSpan, ...]:
        """Address public sentences within this owner's original source ranges."""
        digest = self.manifest(0).sha256
        text = self.public_text()
        return tuple(ContextSpan(digest, sentence)
                     for source in self.source_ranges()
                     for sentence in source.sentences(text))

    def disclosure_for(self, span: ContextSpan):
        from .working_memory_disclosure import WithheldDisclosure

        return WithheldDisclosure()

    def render_into(self, prompt_parts, provider):
        prompt_parts.append(self.text())

    def manifest(self, tokens: int) -> SegmentManifest:
        raw = self.text().encode()
        return SegmentManifest(
            type(self), self.provenance, hashlib.sha256(raw).hexdigest(), len(raw), tokens
        )

    def source_membership(self):
        yield from self.provenance

    def require_source(self, source: Provenance) -> Provenance:
        if source not in self.source_membership():
            raise ValueError("Source is outside the selected context segment")
        return source

    def contribution(self, offset: int, text: str, images=()) -> InputContributionCoordinates:
        raw = text.encode()
        return InputContributionCoordinates(type(self), self.provenance, offset,
                                 len(raw), hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class ContributionCoordinates:
    """Byte coordinates and proof in one owner's rendered public value."""
    kind: type[ContextSegment]
    provenance: tuple[Provenance, ...]
    offset: int
    length: int
    sha256: str

    def __post_init__(self):
        if not self.provenance or min(self.offset, self.length) < 0:
            raise ValueError("Contribution requires original sources and nonnegative byte coordinates")
        if len(self.sha256) != 64 or not set(self.sha256) <= set("0123456789abcdef"):
            raise ValueError("Contribution requires its original byte digest")

    @classmethod
    def capture(cls, kind, provenance, offset, text):
        raw = text.encode("utf-8")
        return cls(kind, provenance, offset, len(raw), hashlib.sha256(raw).hexdigest())

    def public_text(self, original: str) -> str:
        raw = original.encode("utf-8")
        if self.offset + self.length > len(raw):
            raise ValueError("Contribution extends beyond the original public value")
        selected = raw[self.offset:self.offset + self.length]
        if hashlib.sha256(selected).hexdigest() != self.sha256:
            raise ValueError("Contribution differs from its original addressed bytes")
        return selected.decode("utf-8")

    def sentences(self, original: str):
        text = self.public_text(original)
        # Offsets derive from the owned text, never from a later source-file search.
        byte_offset = self.offset
        for sentence in re.split(r"(?<=[.!?])(?=\s)|(?<=\n)", text):
            if sentence.strip():
                yield ContributionCoordinates.capture(
                    self.kind, self.provenance, byte_offset, sentence)
            byte_offset += len(sentence.encode("utf-8"))

    def contains(self, other: ContributionCoordinates) -> bool:
        return ((self.kind, self.provenance) == (other.kind, other.provenance)
                and self.offset <= other.offset
                and other.offset + other.length <= self.offset + self.length)


@dataclass(frozen=True)
class InputContributionCoordinates(ContributionCoordinates):
    """Original INPUT coordinates; image membership remains input-only."""
    images: tuple[int, ...] = ()


@dataclass(frozen=True)
class ContextSpan:
    """An address in a measured segment, never another semantic text store."""

    segment_sha256: str
    coordinates: ContributionCoordinates

    def public_text(self, segment: ContextSegment) -> str:
        if segment.manifest(0).sha256 != self.segment_sha256:
            raise ValueError("Span belongs to a different original segment")
        return self.coordinates.public_text(segment.public_text())


@dataclass(frozen=True, kw_only=True)
class SuppliedSegment:
    """Content supplied by its owner at a decoded external boundary."""

    content: str

    def text(self) -> str:
        return self.content


@dataclass(frozen=True, kw_only=True)
class UserInputSegment(SuppliedSegment, ContextSegment):
    def contribution(self, offset: int, text: str, images=()) -> InputContributionCoordinates:
        return replace(super().contribution(offset, text), images=tuple(range(len(images))))


@dataclass(frozen=True, kw_only=True)
class MeasuredNativeSegment(ContextSegment):
    """An SDK-owned input observation, not a second native message vocabulary."""

    tokens: int
    contributors: tuple[SegmentManifest, ...] = ()

    @abstractmethod
    def provider_value(self): ...

    @abstractmethod
    def render_into(self, prompt_parts, provider): ...

    def text(self):
        return json.dumps(self.provider_value(), ensure_ascii=False, separators=(",", ":"))

    def measured_manifest(self):
        # The SDK measures/hashes its exact original JSON representation.
        return SegmentManifest(type(self), self.provenance,
                               self.sha256, self.utf8_bytes, self.tokens, self.contributors,
                               source_spans=self.assembly_ranges())

    def assembly_ranges(self) -> tuple[ContributionCoordinates, ...]:
        return ()

    def manifest(self, tokens: int) -> SegmentManifest:
        return self.measured_manifest()

    def matches_recorded(self, original: SegmentManifest) -> bool:
        return (self.__class__ is original.kind and self.sha256 == original.sha256
                and self.utf8_bytes == original.utf8_bytes)

    def source_membership(self):
        yield from super().source_membership()
        for contributor in self.contributors:
            yield from contributor.source_membership()

    def public_description(self) -> str:
        return self.measured_manifest().public_description()

    def contributor_manifests(self) -> tuple[SegmentManifest, ...]:
        return self.contributors


@dataclass(frozen=True, kw_only=True)
class SystemLayerSegment(MeasuredNativeSegment):
    content: str
    tokens: int
    sha256: str
    utf8_bytes: int
    source_spans: tuple[ContributionCoordinates, ...] = field(
        default=(), metadata={"wire_omit_default": True})

    def __post_init__(self):
        super().__post_init__()
        offset = 0
        for span in self.source_spans:
            if span.offset != offset:
                raise ValueError("System assembly spans must cover the original value in order")
            span.public_text(self.content)
            offset += span.length
        if self.source_spans and offset != len(self.content.encode("utf-8")):
            raise ValueError("System assembly spans omit original text")

    def assembly_ranges(self) -> tuple[ContributionCoordinates, ...]:
        return self.source_spans

    @classmethod
    def contains_contribution(cls, original: SegmentManifest, coordinates: ContributionCoordinates) -> bool:
        if original.source_spans:
            return any(source.contains(coordinates) for source in original.source_spans)
        # Historical captures prove wording with no assembly attribution.
        return coordinates.provenance == (UnattributedProvenance(),)

    def disclosure_for(self, span: ContextSpan):
        from .working_memory_disclosure import PublicInstructionDisclosure

        return PublicInstructionDisclosure()

    def source_membership(self):
        yield from super().source_membership()
        for span in self.source_spans:
            yield from span.provenance

    def source_ranges(self) -> tuple[ContributionCoordinates, ...]:
        # Old captures prove whole wording, not attribution to today's files.
        return self.source_spans or (ContributionCoordinates.capture(
            type(self), (UnattributedProvenance(),), 0, self.content),)

    def provider_value(self):
        return self.content

    def public_text(self) -> str:
        return self.content

    def render_into(self, prompt_parts, provider):
        provider["systemPrompt"] = self.content


@dataclass(frozen=True, kw_only=True)
class NativeMessages:
    # These are the SDK's exact *provider* messages, including opaque extension
    # and provider fields. Core does not decode them as its event/message subset.
    messages: tuple[dict[str, Any], ...]
    tokens: int
    sha256: str
    utf8_bytes: int

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
        from .pi_payloads import PiMessage

        return "\n".join(PiMessage.from_wire(message).text for message in self.messages)

    def source_ranges(self) -> tuple[ContributionCoordinates, ...]:
        from .pi_payloads import PiMessage

        offset = 0
        ranges = []
        parts = self.recorded_parts(self.measured_manifest())
        for index, message in enumerate(self.messages):
            text = PiMessage.from_wire(message).text
            provenance = parts[index].provenance if parts else self.provenance
            ranges.append(ContributionCoordinates.capture(type(self), provenance, offset, text))
            offset += len(text.encode("utf-8")) + 1
        return tuple(ranges)

    def disclosure_for(self, span: ContextSpan):
        from .pi_payloads import PiMessage

        for source, message in zip(self.source_ranges(), self.messages, strict=True):
            if source.contains(span.coordinates):
                return PiMessage.from_wire(message).annotation_disclosure()
        raise ValueError("Annotation names no original native message")

    def render_into(self, prompt_parts, provider):
        provider.setdefault("messages", []).extend(self.messages)


@dataclass(frozen=True, kw_only=True)
class TranscriptSegment(NativeMessages, MeasuredNativeSegment):
    keyframe_interval: ClassVar[int] = 32

    @classmethod
    def record_manifest(
        cls, original: SegmentManifest, previous: RecordedSegmentManifest | None = None,
        previous_value: SegmentManifest | None = None, previous_offset: int | None = None,
        *, recording_turn: int = 1,
    ) -> tuple[RecordedSegmentManifest, tuple[ContextEntryManifest, ...]]:
        if not original.contributors:
            return super().record_manifest(original, previous, previous_value, previous_offset,
                                           recording_turn=recording_turn)
        provenance = tuple(source for source in original.provenance
                           for source in source.turn_recording())
        header = replace(original, provenance=provenance, contributors=(), captured_text=())
        references = tuple(ContextEntryReference.from_manifest(child)
                           for child in original.contributors)
        prior: tuple[ContextEntryReference, ...] = ()
        keyframe_offset = None
        if (previous is not None and previous_value is not None
                and recording_turn % cls.keyframe_interval):
            prior, keyframe_offset = previous.keyframe(previous_value, previous_offset)
        inherited = 0
        for before, current in zip(prior, references, strict=False):
            if before != current:
                break
            inherited += 1
        entries = tuple(ContextEntryManifest(child) for child in original.contributors[inherited:])
        references = tuple(entry.reference for entry in entries)
        return (
            RecordedSegmentManifest(header, references,
                                    keyframe_offset if inherited else None, inherited),
            entries,
        )


@dataclass(frozen=True, kw_only=True)
class CompactionSummarySegment(NativeMessages, MeasuredNativeSegment):
    def disclosure_for(self, span: ContextSpan):
        from .working_memory_disclosure import PublicInstructionDisclosure

        return PublicInstructionDisclosure()


@dataclass(frozen=True, kw_only=True)
class InjectionMessageSegment(NativeMessages, MeasuredNativeSegment):
    pass


@dataclass(frozen=True, kw_only=True)
class ToolCatalogSegment(MeasuredNativeSegment):
    tools: tuple[dict[str, Any], ...]
    tokens: int
    sha256: str
    utf8_bytes: int

    def provider_value(self):
        return list(self.tools)

    def public_spans(self) -> tuple[ContextSpan, ...]:
        offset = 1
        spans = []
        for tool in self.tools:
            text = json.dumps(tool, ensure_ascii=False, separators=(",", ":"))
            coordinates = ContributionCoordinates.capture(type(self), self.provenance, offset, text)
            spans.append(ContextSpan(self.sha256, coordinates))
            offset += coordinates.length + 1
        return tuple(spans)

    def render_into(self, prompt_parts, provider):
        provider["tools"] = list(self.tools)


@dataclass(frozen=True, kw_only=True)
class InstructionSegment(ContextSegment):
    instruction: InstructionFile

    @classmethod
    def instruction_name(cls):
        return cls.declared_name.replace("_", "-") + ".md"

    @classmethod
    def capture(cls, provenance=(), **values):
        instruction = InstructionFile.read(cls.instruction_name())
        return cls(provenance=(*provenance, instruction.source), instruction=instruction, **values)

    @abstractmethod
    def values(self) -> dict[str, object]: ...

    def text(self) -> str:
        return self.instruction.render(self.values())

    def disclosure_for(self, span: ContextSpan):
        from .working_memory_disclosure import PublicInstructionDisclosure

        return PublicInstructionDisclosure()


@dataclass(frozen=True, kw_only=True)
class TypedUserInputSegment(InstructionSegment, UserInputSegment):
    """Text the user typed into this thread's own view, labelled as such for the model."""

    @classmethod
    def capture(cls, content: str) -> Self:
        return super().capture(content=content)

    def values(self) -> dict[str, object]:
        return {"content": self.content}


class UserRequestSegment(TypedUserInputSegment):
    """The message that starts a turn."""



class UserFollowupSegment(TypedUserInputSegment):
    """A message typed while a turn is running."""



@dataclass(frozen=True)
class PeerState:
    """One relevant peer's identity, relationship and current goal."""

    name: str
    relevance: type[PeerRelevance]
    note: str
    goal: str | None

    @classmethod
    def from_view(cls, view: ThreadView, relation: PeerRelation, goals) -> PeerState:
        goal = view.thread.goal.active() if relation.relevance.grants(GoalShown) else goal_records.NoGoal()
        return cls(
            view.thread.name,
            relation.relevance,
            relation.note,
            goal.require().instructions(goal.require().order.todos(goals, view.thread.incarnation, goal.require().id)) if goal.is_set() else None,
        )


@dataclass(frozen=True, kw_only=True)
class CoordinationSegment(InstructionSegment):
    def needed_after(self, received: tuple[Provenance, ...]) -> bool:
        return not all(source in received for source in self.provenance)

    @classmethod
    def remember_received(cls, child, provenance):
        child.remember_coordination(provenance)

    response_instruction: InstructionFile
    owner: ThreadIncarnation
    parent: str | None
    project: str
    peers: tuple[PeerState, ...]

    @classmethod
    def capture(
        cls, owner: Thread, views: tuple[ThreadView, ...], relations: tuple[PeerRelation, ...], goals
    ) -> CoordinationSegment:
        from .wake_policy import WakePolicy

        response_instruction = WakePolicy.relevance_instruction()
        viewed = {view.thread.incarnation: view for view in views}
        peers = tuple(PeerState.from_view(viewed[r.peer], r, goals)
                      for r in relations if r.peer in viewed and r.relevance.grants(AlwaysListed))
        revision = hashlib.sha256(json.dumps(FieldCodec.encode(peers)).encode()).hexdigest()
        return super().capture(
            (OwnerProvenance(owner.incarnation, revision), response_instruction.source),
            response_instruction=response_instruction,
            owner=owner.incarnation,
            parent=owner.parent,
            project=owner.worktree,
            peers=peers,
        )

    def values(self) -> dict[str, object]:
        return dict(
            response_instruction=self.response_instruction.content,
            name=self.owner.name,
            parent=self.parent,
            project=self.project,
            peers=json.dumps(FieldCodec.encode(self.peers), separators=(",", ":")),
        )

from . import goals as goal_records


@dataclass(frozen=True, kw_only=True)
class GoalSegment(InstructionSegment):
    goal: goal_records.Goal
    orders_text: str = ""

    @classmethod
    def capture(cls, owner: Thread, goal: goal_records.Goal, goals) -> GoalSegment:
        todos = goal.order.todos(goals, owner.incarnation, goal.id)
        orders_text = goal.order.instructions(todos)
        revision = hashlib.sha256(json.dumps(FieldCodec.encode((goal, orders_text))).encode()).hexdigest()
        return super().capture((OwnerProvenance(owner.incarnation, revision),), goal=goal, orders_text=orders_text)

    def values(self) -> dict[str, object]:
        return dict(goal_id=self.goal.id, goal_text=self.goal.render_instructions(self.orders_text), progress=self.goal.progress)


@dataclass(frozen=True, kw_only=True)
class AutomaticTitleSegment(InstructionSegment):
    def values(self) -> dict[str, object]:
        return {}


@dataclass(frozen=True, kw_only=True)
class ReplyRouteSegment(InstructionSegment):
    targets: tuple[str, ...]

    def values(self) -> dict[str, object]:
        return dict(targets=", ".join(self.targets))


@dataclass(frozen=True)
class RenderedInput:
    text: str
    provider: dict[str, Any]
    contributions: tuple[InputContributionCoordinates, ...] = ()


@dataclass(frozen=True)
class ContextTurn(DeclaredFamily, affix="ContextTurn"):
    @abstractmethod
    def source_revision(self, owner: Thread) -> str: ...

    def require_recorded(self) -> RecordedContextTurn:
        raise ValueError("A next-context preview cannot be published as a recorded turn")

    def same_recording(self, other: ContextTurn) -> bool:
        """A recorded allocation keeps its identity across a registry rename."""
        selected = self.require_recorded()
        previous = other.require_recorded()
        return (selected.identity == previous.identity
                and selected.occurrence.generation == previous.occurrence.generation
                and selected.occurrence.incarnation.require_recorded().created_at
                == previous.occurrence.incarnation.require_recorded().created_at)

    def matches_generation(self, generation: int) -> bool:
        return False


@dataclass(frozen=True)
class RecordedContextTurn(ContextTurn):
    identity: TurnId
    occurrence: TurnIdentity

    def __post_init__(self):
        if self.occurrence.generation < 1:
            raise ValueError("Context requires the original authorized turn generation")

    def source_revision(self, owner: Thread) -> str:
        return self.identity.value

    def require_recorded(self) -> RecordedContextTurn:
        return self

    def matches_generation(self, generation: int) -> bool:
        return self.occurrence.generation == generation


class NextContextTurn(ContextTurn):
    def source_revision(self, owner: Thread) -> str:
        return hashlib.sha256(json.dumps(owner.to_wire(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ContextManifest:
    thread: ThreadIncarnation
    turn: ContextTurn
    segments: tuple[SegmentManifest, ...]
    counter: str
    # Original request-owner correlation, absent for previews and captures that
    # never observed dispatch. It permits neither new input nor replay.
    request_id: str | None = field(default=None, metadata={"wire_omit_default": True})

    @property
    def public_text_recorded(self) -> bool:
        return bool(self.segments) and all(segment.public_text_recorded for segment in self.segments)

    def require_request_id(self) -> str:
        if not self.request_id:
            raise ValueError("Original request ID was not captured by this historical context")
        return self.request_id

    def selected_segment(self, position: int, contributors: tuple[int, ...] = ()) -> SegmentManifest:
        if not 0 <= position < len(self.segments):
            raise ValueError("Original request has no selected segment")
        return self.segments[position].selected_contributor(contributors)

    def require_source(self, source: Provenance) -> Provenance:
        for segment in self.segments:
            if source in segment.source_membership():
                return source
        raise ValueError("Source is outside the original recorded context")

    @classmethod
    def for_request(cls, history, turn: RecordedContextTurn, request_id: str) -> ContextManifest:
        """Select the original indexed publication, never today's preview."""
        selected = tuple(record for record in history
                         if record.turn == turn and record.request_id == request_id)
        if not request_id or len(selected) != 1:
            raise ValueError("Original context request is absent or ambiguous")
        return selected[0]

    def public_source_text(self, comms, segment: int, source: Provenance) -> ContextSourceText:
        if source not in self.selected_segment(segment).source_membership():
            raise ValueError("Source is outside the original recorded context segment")
        return ContextSourceText(source.public_description(), source.public_text(comms))

    def changed_since(self, previous: ContextManifest) -> ContextManifestChange:
        """An inspection projection, never a retained input copy."""
        return ContextManifestChange(
            previous.turn, self.turn,
            tuple(s for s in previous.segments if s not in self.segments),
            tuple(s for s in self.segments if s not in previous.segments))


@dataclass(frozen=True)
class ContextManifestChange(CliResult):
    previous_turn: ContextTurn
    turn: ContextTurn
    removed: tuple[SegmentManifest, ...]
    added: tuple[SegmentManifest, ...]


@dataclass(frozen=True)
class RecordedContextManifest:
    """One turn's context identity and references to separately recorded entries."""

    thread: ThreadIncarnation
    turn: ContextTurn
    segments: tuple[RecordedSegmentManifest, ...]
    counter: str
    request_id: str | None = field(default=None, metadata={"wire_omit_default": True})
    recording_turn: int = field(default=1, metadata={"wire_omit_default": True})

    @classmethod
    def capture(
        cls, manifest: ContextManifest,
        previous: CapturedWireSource[RecordedContextManifest, ContextManifest] | None = None,
    ) -> tuple[RecordedContextManifest, tuple[ContextEntryManifest, ...]]:
        recording_turn = 1
        prior: tuple[RecordedSegmentManifest, ...] = ()
        if previous is not None:
            previous_record = previous.pointer.decode_bytes(previous.raw, previous.source.root_id)
            prior = previous_record.segments
            recording_turn += previous_record.recording_turn
        parts = []
        for position, segment in enumerate(manifest.segments):
            old = value = offset = None
            if previous is not None and position < len(previous.value.segments):
                candidate = previous.value.segments[position]
                if candidate.kind is segment.kind:
                    value = candidate
                    old = prior[position]
                    offset = previous.pointer.offset
            parts.append(segment.kind.record_manifest(
                segment, old, value, offset, recording_turn=recording_turn))
        return (
            cls(
                manifest.thread,
                manifest.turn,
                tuple(part for part, _ in parts),
                manifest.counter,
                manifest.request_id,
                recording_turn,
            ),
            tuple(entry for _, group in parts for entry in group),
        )

    def restore(self, entries: dict[ContextEntryReference, SegmentManifest],
                previous: dict[int, RecordedContextManifest] | None = None) -> ContextManifest:
        previous = {} if previous is None else previous
        return ContextManifest(
            self.thread,
            self.turn,
            tuple(segment.restore(entries, previous, position)
                  for position, segment in enumerate(self.segments)),
            self.counter,
            self.request_id,
        )


@dataclass(frozen=True)
class TurnContext(DeclaredReply):
    thread: ThreadIncarnation
    turn: ContextTurn
    segments: tuple[ContextSegment, ...]

    @classmethod
    def for_inspection(cls, comms, owner: Thread) -> TurnContext:
        """The current Core contributors, before any future original input.

        This is a preview through the same contributor declarations used by
        preparation. It is never a recorded SDK request or an input grant.
        """
        relations = comms.relationships.context_relations(owner.name)
        context = cls.for_owner(owner, NextContextTurn(), "", comms.views.thread_views(), relations, comms.goals)
        for segment in owner.context_goal_segments(comms.goals):
            context = context.prepend(segment)
        return context.with_awareness(comms, owner, relations, blocking=True)

    def with_awareness(
        self, comms, owner: Thread, relations: tuple[PeerRelation, ...], *, blocking: bool = False
    ) -> TurnContext:
        """Append what the owner should know without being asked.

        Its relationships, the messages addressed to it and its partners' goal changes.
        """
        from .context_segments.partner_activity import PartnerActivitySegment
        from .context_segments.relationships import RelationshipsSegment

        context = self
        for segment in (
            *RelationshipsSegment.capture(owner, relations),
            *comms.bus.awareness_segments(owner, relations, blocking=blocking),
            *PartnerActivitySegment.capture(owner, relations, comms.goals),
        ):
            context = context.append(segment)
        return context

    def require_source(self, source: Provenance) -> Provenance:
        for segment in self.segments:
            if source in segment.source_membership():
                return source
        raise ValueError("Source is outside the selected context preview")

    def public_source_text(self, comms, owner: ThreadIncarnation, segment: int,
                           manifest: SegmentManifest, source: Provenance) -> ContextSourceText:
        """Read only a source selected from this current Core observation."""
        if owner != self.thread:
            raise ValueError("Current Core context belongs to another owner incarnation")
        if not 0 <= segment < len(self.segments):
            raise ValueError("Source has no selected Core context segment")
        selected = self.segments[segment]
        if selected.manifest(0) != manifest:
            raise ValueError("Current Core context segment changed since selection")
        original = selected.require_source(source)
        return ContextSourceText(original.public_description(), original.public_text(comms))

    @classmethod
    def for_owner(
        cls,
        owner: Thread,
        turn: ContextTurn,
        task: str,
        views: tuple[ThreadView, ...],
        relations: tuple[PeerRelation, ...],
        goals,
        origins: tuple[MessageReference, ...] = (),
        inputs: tuple[StoredInput, ...] = (),
    ) -> TurnContext:
        source = tuple(WireProvenance(ref) for ref in origins) or (
            OwnerProvenance(owner.incarnation, turn.source_revision(owner)),
        )
        source += tuple(row.context_provenance() for row in inputs)
        return cls(
            owner.incarnation,
            turn,
            (
                CoordinationSegment.capture(owner, views, relations, goals),
                UserInputSegment(provenance=source, content=task),
            ),
        )

    def prepend(self, segment: ContextSegment) -> TurnContext:
        return replace(self, segments=(segment, *self.segments))

    def append(self, segment: ContextSegment) -> TurnContext:
        return replace(self, segments=(*self.segments, segment))

    def after_received(self, received: tuple[Provenance, ...]) -> TurnContext:
        return replace(self, segments=tuple(segment for segment in self.segments
                                           if segment.needed_after(received)))

    def render(self, *, images=()) -> RenderedInput:
        return self.render_segments(self.segments, images=images)

    @staticmethod
    def render_segments(segments: tuple[ContextSegment, ...], *, images=()) -> RenderedInput:
        prompt_parts, provider, contributions = [], {}, []
        offset = 0
        for segment in segments:
            start = len(prompt_parts)
            segment.render_into(prompt_parts, provider)
            if len(prompt_parts) > start:
                contribution = segment.contribution(offset, "".join(prompt_parts[start:]), images)
                contributions.append(contribution)
                offset += contribution.length
        return RenderedInput("".join(prompt_parts), provider, tuple(contributions))

