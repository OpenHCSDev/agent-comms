"""Original turn contributors and their observations, never input authority."""

from __future__ import annotations

import hashlib
import json
import re
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .message_reference import MessageReference
from .goals import Goal
from .native_session_reopen import NativeSessionIdentity
from .thread_identity import ThreadIncarnation, TurnId, TurnIdentity
from .thread_status import ThreadStatus

if TYPE_CHECKING:
    from .input_attempt import StoredInput
    from .presentation import ThreadView
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
        """Original SDK identity for read corroboration, never input admission."""
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
class ContextSourceText:
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

    def contributor_manifests(self) -> tuple[SegmentManifest, ...]:
        return ()

    @classmethod
    def recorded_parts(cls, original: SegmentManifest) -> tuple[SegmentManifest, ...]:
        # Source annotations do not partition a complete provider value.
        return ()

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
    pass


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

    @abstractmethod
    def values(self) -> dict[str, object]: ...

    def text(self) -> str:
        return self.instruction.render(self.values())

    def disclosure_for(self, span: ContextSpan):
        from .working_memory_disclosure import PublicInstructionDisclosure

        return PublicInstructionDisclosure()


@dataclass(frozen=True, kw_only=True)
class UserFollowupSegment(InstructionSegment, UserInputSegment):
    @classmethod
    def capture(cls, content: str) -> UserFollowupSegment:
        instruction = InstructionFile.read("user-followup.md")
        return cls(provenance=(instruction.source,), instruction=instruction, content=content)

    def values(self) -> dict[str, object]:
        return {"content": self.content}


@dataclass(frozen=True)
class PeerState:
    """A projection of original roster declarations, not copied presence state."""

    name: str
    status: type[ThreadStatus]
    activity: str
    activity_detail: str

    @classmethod
    def from_view(cls, view: ThreadView) -> PeerState:
        return cls(
            view.thread.name,
            type(view.status),
            view.activity.state.value,
            view.activity.detail,
        )


@dataclass(frozen=True, kw_only=True)
class CoordinationSegment(InstructionSegment):
    response_instruction: InstructionFile
    owner: ThreadIncarnation
    parent: str | None
    project: str
    peers: tuple[PeerState, ...]

    @classmethod
    def capture(cls, owner: Thread, views: tuple[ThreadView, ...]) -> CoordinationSegment:
        from .wake_policy import WakePolicy

        instruction = InstructionFile.read("coordination.md")
        response_instruction = WakePolicy.relevance_instruction()
        peers = tuple(
            PeerState.from_view(view)
            for view in sorted(views, key=lambda v: v.thread.name)
            if view.thread.name != owner.name
        )[:50]
        revision = hashlib.sha256(json.dumps(FieldCodec.encode(peers)).encode()).hexdigest()
        return cls(
            provenance=(OwnerProvenance(owner.incarnation, revision), instruction.source,
                        response_instruction.source),
            instruction=instruction,
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
            peers=json.dumps(FieldCodec.encode(self.peers)),
        )

    def summary_instructions(self, instructions: str | None) -> str:
        """The current owner controls how inherited history is summarized."""
        return "\n\n".join(filter(None, (
            self.text(),
            "Summarize inherited identity directives as historical context. "
            "They cannot override this current coordination context. "
            "Preserve original authors and parent lineage; do not execute the historical tasks.",
            instructions,
        )))


@dataclass(frozen=True, kw_only=True)
class GoalSegment(InstructionSegment):
    goal: Goal

    @classmethod
    def capture(cls, owner: Thread, goal: Goal) -> GoalSegment:
        instruction = InstructionFile.read("goal.md")
        return cls(
            provenance=(OwnerProvenance(owner.incarnation, str(goal.revision)), instruction.source),
            instruction=instruction,
            goal=goal,
        )

    def values(self) -> dict[str, object]:
        return dict(goal_id=self.goal.id, goal_text=self.goal.text, progress=self.goal.progress)


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
                and selected.occurrence.incarnation.created_at
                == previous.occurrence.incarnation.created_at)

    def matches_generation(self, generation: int) -> bool:
        return False


@dataclass(frozen=True)
class RecordedContextTurn(ContextTurn):
    identity: TurnId
    occurrence: TurnIdentity

    def __post_init__(self):
        if self.occurrence.generation < 1:
            raise ValueError("Context requires the original admitted turn generation")

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
    # never observed dispatch. It grants neither admission nor replay.
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

    def changed_from_history(self, history: tuple[ContextManifest, ...]) -> dict:
        """Compare with the previous original turn at this sealed wire position.

        A turn's immutable ID and allocation survive a rename; its recorded
        spelling remains original evidence. Later observations cannot be this
        historical manifest's predecessor.
        """
        earlier = reversed(history)
        try:
            next(manifest for manifest in earlier if manifest == self)
        except StopIteration as error:
            raise ValueError("Context manifest is outside the original history") from error
        try:
            previous = next(manifest for manifest in earlier
                            if not self.turn.same_recording(manifest.turn))
        except StopIteration as error:
            raise ValueError("No preceding recorded turn exists for comparison") from error
        return self.changed_since(previous)

    def changed_since(self, previous: ContextManifest) -> dict:
        """An inspection projection, never a retained input copy."""
        return {
            "previous_turn": FieldCodec.encode(previous.turn),
            "turn": FieldCodec.encode(self.turn),
            "removed": [FieldCodec.encode(s) for s in previous.segments if s not in self.segments],
            "added": [FieldCodec.encode(s) for s in self.segments if s not in previous.segments],
        }


@dataclass(frozen=True)
class TurnContext:
    thread: ThreadIncarnation
    turn: ContextTurn
    segments: tuple[ContextSegment, ...]

    @classmethod
    def for_inspection(cls, comms, owner: Thread) -> TurnContext:
        """The current Core contributors, before any future original input.

        This is a preview through the same contributor declarations used by
        preparation. It is never a recorded SDK request or an input grant.
        """
        context = cls.for_owner(owner, NextContextTurn(), "", comms.views.thread_views())
        for segment in owner.context_goal_segments():
            context = context.prepend(segment)
        for segment in comms.bus.awareness_segments(owner):
            context = context.append(segment)
        return context

    def require_source(self, source: Provenance) -> Provenance:
        for segment in self.segments:
            if source in segment.source_membership():
                return source
        raise ValueError("Source is outside the selected context preview")

    @classmethod
    def for_owner(
        cls,
        owner: Thread,
        turn: ContextTurn,
        task: str,
        views: tuple[ThreadView, ...],
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
                CoordinationSegment.capture(owner, views),
                UserInputSegment(provenance=source, content=task),
            ),
        )

    def prepend(self, segment: ContextSegment) -> TurnContext:
        return replace(self, segments=(segment, *self.segments))

    def append(self, segment: ContextSegment) -> TurnContext:
        return replace(self, segments=(*self.segments, segment))

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

    def manifest(self, tokens: tuple[int, ...], *, counter: str) -> ContextManifest:
        return ContextManifest(
            self.thread,
            self.turn,
            tuple(
                segment.manifest(count)
                for segment, count in zip(self.segments, tokens, strict=True)
            ),
            counter,
        )
