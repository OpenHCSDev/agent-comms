from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, ClassVar, Self

from pydantic import JsonValue

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.errors import RelationViolationError
from agent_comms.execution.annotation_disclosure import (
    PublicInstructionAnnotations,
    WithheldAnnotations,
)
from agent_comms.execution.context_owner import ContextOwner
from agent_comms.execution.context_provenance_source import ContextProvenanceSource
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.execution.turn_item import NativeItemId, PiRequestKey
from agent_comms.instruction_bytes import InstructionBytes
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    IdentityExclude,
    NullMember,
    Sha256Text,
    SingleFieldValue,
    SubclassUnion,
    TurnId,
    WireName,
    WireOmitDefault,
)
from agent_comms.peer_relevance import AlwaysListed, PeerRelevance
from agent_comms.planning.goal_mutation import GoalContextSource
from agent_comms.planning.goal_work import GoalWorkSource
from agent_comms.planning.goals import Goal, NoGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.peer_relation import PeerRelation
from agent_comms.registry.thread_identity import ThreadIncarnation, TurnIdentity
from agent_comms.transport.tool_result import DeclaredReply, ToolResult
from agent_comms.waking.wake_policy import WakePolicy


@dataclass(frozen=True)
class Provenance(DeclaredFamily, affix="Provenance", discovery_package="agent_comms.cli_bridges.codex_records"):
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

    def bus_references(self) -> tuple[MessageReference, ...]:
        """Only a wire coordinate names a committed bus message."""
        return ()

    def require_human_input(self):
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


class HistoricalInstructionProvenance(Provenance, declared_name=None):
    """A retained instruction from a source session, never current execution authority."""

    @abstractmethod
    def instruction_position(self) -> tuple[int, int]:
        """Its original record and instruction ordering coordinates."""
        ...


@dataclass(frozen=True)
class OwnerProvenance(Provenance):
    owner: ThreadIncarnation
    revision: str

    def public_description(self) -> str:
        return f"Owner {self.owner.name} · born {self.owner.created_at} · revision {self.revision}"


@dataclass(frozen=True)
class TurnProvenance(Provenance):
    """The owner's recorded turn itself, never a digest standing in for it."""

    owner: ThreadIncarnation
    turn: TurnId

    def public_description(self) -> str:
        return f"Owner {self.owner.name} · born {self.owner.created_at} · turn {self.turn}"


@dataclass(frozen=True)
class WireProvenance(Provenance):
    source: MessageReference

    def public_description(self) -> str:
        return f"Wire message {self.source.seq} · {self.source.message_id}"

    def public_text(self, comms) -> str:
        (original,) = comms.bus.bus.messages_for_references((self.source,))
        return original.body

    def bus_references(self) -> tuple[MessageReference, ...]:
        return (self.source,)


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
class ContextSourceText(ToolResult):
    """A public source-read result, not another context or input record."""

    description: str
    text: str

    def span_text(self, coordinates: ContributionCoordinates) -> str:
        """A span's own bytes, proved against this original text."""
        return coordinates.public_text(self.text)


@dataclass(frozen=True)
class UnretainedSourceText(ContextSourceText):
    """A saved record that kept no public text. Its text says so, and a span cannot be proved
    against a notice, so a span reads as the notice too."""

    text: str = (
        "Public SDK text was not retained in this saved record; no live preview is available."
    )

    def span_text(self, coordinates: ContributionCoordinates) -> str:
        return self.text


@dataclass(frozen=True)
class InstructionFile:
    """One immutable read used for both rendering and file provenance."""

    content: str
    source: FileProvenance

    @classmethod
    def read(cls, name: str) -> InstructionFile:
        read = InstructionBytes.read(name)
        return cls(read.text, FileProvenance(str(read.path), hashlib.sha256(read.raw).hexdigest()))

    def render(self, values: dict[str, object]) -> str:
        return self.content.format_map(values)


@dataclass(frozen=True)
class ContextSources:
    """Find native journal positions from the records contributing to this context."""

    provenance: tuple[Provenance, ...]

    def bus_references(self) -> tuple[MessageReference, ...]:
        return tuple(reference for source in self.provenance for reference in source.bus_references())

    def native_identity(self) -> NativeSessionIdentity:
        originals = {identity for source in self.provenance for identity in source.native_identities()}
        if len(originals) != 1:
            raise ValueError("Original segment has no unambiguous native source identity")
        (identity,) = originals
        return identity

    def journal_coordinates(self) -> tuple[str, ...]:
        identity = self.native_identity()
        return tuple(dict.fromkeys(entry for source in self.provenance
                                   for entry in source.journal_entries(identity)))

    def journal_entries(self) -> tuple[str, ...]:
        entries = self.journal_coordinates()
        if not entries:
            raise ValueError("Original segment has no recoverable journal coordinates")
        return entries


@dataclass(frozen=True)
class SegmentManifest(ToolResult):
    kind: type[ContextSegment]
    provenance: tuple[Provenance, ...]
    sha256: str
    utf8_bytes: int
    tokens: int
    contributors: tuple[SegmentManifest, ...] = ()
    captured_text: Annotated[tuple[str, ...], WireOmitDefault()] = ()
    source_spans: Annotated[
        tuple[ContributionCoordinates, ...], WireOmitDefault()
    ] = ()

    def __post_init__(self):
        if not self.provenance or min(self.utf8_bytes, self.tokens) < 0:
            raise ValueError("Invalid original context segment observation")
        Sha256Text.from_text(self.sha256)

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
        """Match an annotation by its segment hash and position, not by text alone."""
        return (span.segment_sha256 == self.sha256
                and span.coordinates.kind is self.kind
                and self.kind.contains_contribution(self, span.coordinates))

    def with_entries(self, entries: tuple[SegmentManifest, ...]) -> SegmentManifest:
        """This header with the entries it was recorded or sent without: each entry's
        journal source is one of the whole segment's sources."""
        membership = tuple(source for entry in entries
                           for source in entry.provenance
                           for source in source.entry_membership())
        return replace(self, provenance=(*self.provenance, *membership), contributors=entries)

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

    @property
    def sources(self) -> ContextSources:
        return ContextSources(self.provenance)

    def wire_sources(self) -> tuple[MessageReference, ...]:
        """Original bus messages declared by this segment, in contribution order."""
        return self.kind.wire_sources(self)

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

    def saved_source_text(self) -> ContextSourceText:
        """What a saved record of this segment can show: its retained text, or that none was kept.
        A saved record has no live owner to read the rest from."""
        if self.public_text_recorded:
            return ContextSourceText(self.public_description(), self.recorded_public_text({}))
        return UnretainedSourceText(self.public_description())


@dataclass(frozen=True)
class ContextEntryReference:
    """One native journal entry whose manifest is recorded independently."""

    identity: NativeSessionIdentity
    entry_id: NativeItemId

    @classmethod
    def from_sources(cls, sources: ContextSources) -> ContextEntryReference:
        entries = sources.journal_entries()
        if len(entries) != 1:
            raise ValueError("A context entry manifest must name exactly one journal entry")
        return cls(sources.native_identity(), NativeItemId(entries[0]))


@dataclass(frozen=True)
class ContextEntryManifest:
    """The one recorded manifest for a native transcript entry."""

    manifest: SegmentManifest

    @property
    def reference(self) -> ContextEntryReference:
        return ContextEntryReference.from_sources(self.manifest.sources)


@dataclass(frozen=True)
class SentWhole:
    """What a reader holds of one segment after its Pi process sent it, which the process's
    next observation at that position is sent after. A kind is sent whole each time: the
    reader holds none of its entries, and the name of none is the empty mark."""

    entries: tuple[SegmentManifest, ...] = ()
    runs: Mapping[str, int] = field(default_factory=lambda: {"": 0})
    """How many leading entries each mark names."""

    def named(self, mark: str) -> tuple[SegmentManifest, ...]:
        if mark not in self.runs:
            raise ValueError("A segment was sent after entries its process never sent here")
        return self.entries[:self.runs[mark]]

    def then(self, mark: str, sent: SegmentManifest) -> Self:
        """What the reader holds once ``sent`` arrives after the entries ``mark`` names."""
        self.named(mark)
        return self

    def whole(self, sent: SegmentManifest) -> SegmentManifest:
        return sent


@dataclass(frozen=True)
class SentEntries(SentWhole):
    """A kind whose process sends each entry once (``TranscriptSegment.sent`` in
    stack/native): 220 transcript entries are not sent again with every request. The next
    observation names the leading entries it sent before by their mark, the hash chain of
    each entry's journal identity and digest, and sends the rest. A reader that holds other
    entries does not know the mark, so it cannot take them for the ones named."""

    def then(self, mark: str, sent: SegmentManifest) -> Self:
        entries = [*self.named(mark)]
        runs = {name: length for name, length in self.runs.items() if length <= len(entries)}
        for entry in sent.contributors:
            identity = ContextEntryReference.from_sources(entry.sources).entry_id
            mark = hashlib.sha256(f"{mark}\n{identity}:{entry.sha256}".encode()).hexdigest()
            entries.append(entry)
            runs[mark] = len(entries)
        return replace(self, entries=tuple(entries), runs=runs)

    def whole(self, sent: SegmentManifest) -> SegmentManifest:
        return sent.with_entries(self.entries)


_NO_CONTEXT_RECORDS: Mapping[int, RecordedContextManifest] = MappingProxyType({})


class ContextPredecessor(SubclassUnion, affix="ContextPredecessor"):
    """An absent predecessor or the position that owns its lookup."""

    def require_direct(self) -> None:
        raise ValueError("Recorded segment predecessor is not a direct keyframe")

    def require_predecessor_for_inherited(self, inherited_entries: int) -> None:
        pass

    @abstractmethod
    def predecessors(self, offset: int) -> tuple[int, ...]: ...

    @abstractmethod
    def at_keyframe(self, offset: int) -> RecordedContextPredecessor: ...

    @abstractmethod
    def require_previous(self) -> RecordedContextPredecessor: ...

    @abstractmethod
    def keyframe_count(self, entries: int, inherited: int) -> int: ...

    @abstractmethod
    def recorded(
        self, records: Mapping[int, RecordedContextManifest], position: int,
        kind: type[ContextSegment],
    ) -> RecordedSegmentManifest: ...

    @abstractmethod
    def recording(
        self, original: RecordedSegmentManifest,
        records: Mapping[int, RecordedContextManifest],
        entries: Mapping[ContextEntryReference, SegmentManifest], position: int,
    ) -> SegmentRecording: ...


@dataclass(frozen=True)
class NoContextPredecessor(NullMember, ContextPredecessor):
    def require_direct(self) -> None:
        pass

    def require_predecessor_for_inherited(self, inherited_entries: int) -> None:
        if inherited_entries:
            raise ValueError("Recorded transcript prefix requires its predecessor")

    def predecessors(self, offset: int) -> tuple[int, ...]:
        return ()

    def at_keyframe(self, offset: int) -> RecordedContextPredecessor:
        return RecordedContextPredecessor(offset)

    def require_previous(self) -> RecordedContextPredecessor:
        raise ValueError("Recorded text predecessor is missing")

    def keyframe_count(self, entries: int, inherited: int) -> int:
        return entries

    def recorded(
        self, records: Mapping[int, RecordedContextManifest], position: int,
        kind: type[ContextSegment],
    ) -> RecordedSegmentManifest:
        raise ValueError("Recorded segment predecessor is missing")

    def recording(
        self, original: RecordedSegmentManifest,
        records: Mapping[int, RecordedContextManifest],
        entries: Mapping[ContextEntryReference, SegmentManifest], position: int,
    ) -> SegmentRecording:
        return UnrecordedSegment()


@dataclass(frozen=True)
class RecordedContextPredecessor(SingleFieldValue, ContextPredecessor):
    record_id: int

    def predecessors(self, offset: int) -> tuple[int, ...]:
        if not 0 <= self.record_id < offset:
            raise ValueError("Context predecessor is not an earlier record")
        return (self.record_id,)

    def at_keyframe(self, offset: int) -> RecordedContextPredecessor:
        return self

    def require_previous(self) -> RecordedContextPredecessor:
        return self

    def keyframe_count(self, entries: int, inherited: int) -> int:
        return inherited

    def recorded(
        self, records: Mapping[int, RecordedContextManifest], position: int,
        kind: type[ContextSegment],
    ) -> RecordedSegmentManifest:
        try:
            prior = records[self.record_id].segments[position]
        except (KeyError, IndexError) as error:
            raise ValueError("Recorded segment predecessor is missing") from error
        prior.previous.require_direct()
        if prior.manifest.kind is not kind:
            raise ValueError("Recorded segment predecessor has a different kind")
        return prior

    def recording(
        self, original: RecordedSegmentManifest,
        records: Mapping[int, RecordedContextManifest],
        entries: Mapping[ContextEntryReference, SegmentManifest], position: int,
    ) -> SegmentRecording:
        prior = self.recorded(records, position, original.manifest.kind)
        return PreviouslyRecordedSegment(prior, prior.restore(entries), self.record_id)


@dataclass(frozen=True)
class RecordedSegmentManifest:
    """A turn segment with transcript contributors held by entry reference."""

    manifest: SegmentManifest
    entries: tuple[ContextEntryReference, ...] = ()
    previous: Annotated[ContextPredecessor, WireOmitDefault()] = field(
        default_factory=NoContextPredecessor)
    inherited_entries: Annotated[int, WireOmitDefault()] = 0

    def __post_init__(self) -> None:
        if self.entries and self.manifest.contributors:
            raise ValueError("Recorded segment cannot inline and reference contributors")
        if self.inherited_entries < 0:
            raise ValueError("Recorded transcript prefix requires its predecessor")
        self.previous.require_predecessor_for_inherited(self.inherited_entries)

    def predecessors(self, offset: int) -> tuple[int, ...]:
        return self.previous.predecessors(offset)

    def keyframe(
        self, original: SegmentManifest, offset: int,
    ) -> tuple[tuple[ContextEntryReference, ...], RecordedContextPredecessor]:
        count = self.previous.keyframe_count(len(self.entries), self.inherited_entries)
        references = tuple(ContextEntryReference.from_sources(child.sources)
                           for child in original.contributors[:count])
        return references, self.previous.at_keyframe(offset)

    def record_text(
        self, original: SegmentManifest, previous_value: SegmentManifest, offset: int,
    ) -> RecordedSegmentManifest:
        if (original.sha256 == previous_value.sha256
                and original.captured_text and previous_value.captured_text):
            reference = (RecordedContextPredecessor(offset) if self.manifest.captured_text
                         else self.previous.require_previous())
            return RecordedSegmentManifest(replace(original, captured_text=()), previous=reference)
        return RecordedSegmentManifest(original)

    def predecessor(
        self, predecessors: Mapping[int, RecordedContextManifest], position: int,
    ) -> RecordedSegmentManifest:
        return self.previous.recorded(predecessors, position, self.manifest.kind)

    def restore(
        self, entries: Mapping[ContextEntryReference, SegmentManifest],
        predecessors: Mapping[int, RecordedContextManifest] = _NO_CONTEXT_RECORDS,
        position: int = 0,
    ) -> SegmentManifest:
        return self.previous.recording(self, predecessors, entries, position).restore(self, entries)


class SegmentRecording(ABC):
    """The previous value needed to record or restore one segment."""

    @abstractmethod
    def record_text(self, original: SegmentManifest) -> RecordedSegmentManifest: ...

    @abstractmethod
    def record_entries(
        self, header: SegmentManifest, references: tuple[ContextEntryReference, ...],
    ) -> RecordedSegmentManifest: ...

    @abstractmethod
    def restore_text(self, original: SegmentManifest) -> SegmentManifest: ...

    @abstractmethod
    def inherited(self, count: int) -> tuple[SegmentManifest, ...]: ...

    def restore(
        self, recorded: RecordedSegmentManifest,
        entries: Mapping[ContextEntryReference, SegmentManifest],
    ) -> SegmentManifest:
        original = self.restore_text(recorded.manifest)
        if not recorded.entries and not recorded.inherited_entries:
            return original
        return original.with_entries((
            *self.inherited(recorded.inherited_entries),
            *(entries[reference] for reference in recorded.entries)))


class UnrecordedSegment(SegmentRecording):
    def record_text(self, original: SegmentManifest) -> RecordedSegmentManifest:
        return RecordedSegmentManifest(original)

    def record_entries(
        self, header: SegmentManifest, references: tuple[ContextEntryReference, ...],
    ) -> RecordedSegmentManifest:
        return RecordedSegmentManifest(header, references)

    def restore_text(self, original: SegmentManifest) -> SegmentManifest:
        return original

    def inherited(self, count: int) -> tuple[SegmentManifest, ...]:
        if count:
            raise ValueError("Recorded transcript prefix is missing")
        return ()


@dataclass(frozen=True)
class PreviouslyRecordedSegment(SegmentRecording):
    recorded: RecordedSegmentManifest
    value: SegmentManifest
    record_id: int

    def record_text(self, original: SegmentManifest) -> RecordedSegmentManifest:
        return self.recorded.record_text(original, self.value, self.record_id)

    def record_entries(
        self, header: SegmentManifest, references: tuple[ContextEntryReference, ...],
    ) -> RecordedSegmentManifest:
        prior, keyframe = self.recorded.keyframe(self.value, self.record_id)
        inherited = 0
        for before, current in zip(prior, references, strict=False):
            if before != current:
                break
            inherited += 1
        if inherited:
            return RecordedSegmentManifest(header, references[inherited:], keyframe, inherited)
        return RecordedSegmentManifest(header, references)

    def restore_text(self, original: SegmentManifest) -> SegmentManifest:
        if self.value.sha256 == original.sha256 and not original.captured_text:
            return replace(original, captured_text=self.value.captured_text)
        return original

    def inherited(self, count: int) -> tuple[SegmentManifest, ...]:
        if count > len(self.value.contributors):
            raise ValueError("Recorded transcript prefix is missing")
        return self.value.contributors[:count]


@dataclass(frozen=True, kw_only=True)
class ContextSegment(
    WithheldAnnotations, DeclaredFamily, affix="Segment", discovery_package="agent_comms.context_segments"
):
    provenance: tuple[Provenance, ...]

    sending: ClassVar[type[SentWhole]] = SentWhole
    """How a Pi process sends this kind, observation after observation."""

    def __post_init__(self) -> None:
        if not self.provenance:
            raise TypeError("A context contribution requires its original source")

    @abstractmethod
    def text(self) -> str: ...

    @classmethod
    def sent_once(
        cls, texts: dict[tuple[type[ContextSegment], str], tuple[str, ...]],
        manifest: SegmentManifest,
    ) -> SegmentManifest:
        """``manifest`` as its request records it, given the ``texts`` its process sent
        before. A kind arrives whole unless it says otherwise."""
        return manifest

    @classmethod
    def public_title(cls) -> str:
        return cls.display_label()

    def public_description(self) -> str:
        return self.public_title()

    def needed_after(self, received: tuple[Provenance, ...]) -> bool:
        return True

    @classmethod
    def remember_received(cls, child, provenance):
        pass

    @classmethod
    def wire_sources(cls, original: SegmentManifest) -> tuple[MessageReference, ...]:
        """A segment without its own wire sources answers through its contributors."""
        return tuple(source for child in original.contributors for source in child.wire_sources())

    def contributor_manifests(self) -> tuple[SegmentManifest, ...]:
        return ()

    @classmethod
    def recorded_parts(cls, original: SegmentManifest) -> tuple[SegmentManifest, ...]:
        # Source annotations do not partition a complete provider value.
        return ()

    @classmethod
    def record_manifest(
        cls, original: SegmentManifest, previous: SegmentRecording,
        *, recording_turn: int = 1,
    ) -> tuple[RecordedSegmentManifest, tuple[ContextEntryManifest, ...]]:
        return previous.record_text(original), ()

    def public_text(self) -> str:
        return self.text()

    def source_ranges(self) -> tuple[ContributionCoordinates, ...]:
        return (ContributionCoordinates.capture(
            type(self), self.provenance, 0, self.public_text()),)

    @classmethod
    def contains_contribution(cls, original: SegmentManifest, coordinates: ContributionCoordinates) -> bool:
        return all(source in original.source_membership() for source in coordinates.provenance)

    def public_spans(self) -> tuple[ContextSpan, ...]:
        """Locate public sentences within the text ranges supplied by this segment."""
        digest = self.manifest(0).sha256
        text = self.public_text()
        return tuple(ContextSpan(digest, sentence)
                     for source in self.source_ranges()
                     for sentence in source.sentences(text))

    def render_into(self, prompt_parts, provider):
        prompt_parts.append(self.text())
        return provider

    def manifest(self, tokens: int) -> SegmentManifest:
        raw = self.text().encode()
        return SegmentManifest(
            kind=type(self), provenance=self.provenance, sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw), tokens=tokens
        )

    def source_membership(self):
        yield from self.provenance

    def require_source(self, source: Provenance) -> Provenance:
        if source not in self.source_membership():
            raise ValueError("Source is outside the selected context segment")
        return source

    def contribution(self, offset: int, text: str, images=()) -> InputContributionCoordinates:
        raw = text.encode()
        return InputContributionCoordinates(
            type(self), self.provenance,
            offset=offset, length=len(raw), sha256=hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True, kw_only=True)
class DigestedSpan:
    """A byte range and its SHA-256: position + integrity in one declaration."""

    offset: int
    length: int
    sha256: str


@dataclass(frozen=True)
class ContributionCoordinates(DigestedSpan):
    """Byte coordinates and proof in one owner's rendered public value."""
    kind: type[ContextSegment]
    provenance: tuple[Provenance, ...]

    def __post_init__(self):
        if not self.provenance or min(self.offset, self.length) < 0:
            raise ValueError("Contribution requires original sources and nonnegative byte coordinates")
        Sha256Text.from_text(self.sha256)

    @classmethod
    def capture(cls, kind, provenance, offset, text):
        raw = text.encode("utf-8")
        return cls(kind=kind, provenance=provenance, offset=offset, length=len(raw), sha256=hashlib.sha256(raw).hexdigest())

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
    """Record positions in submitted input, including only images from that input."""
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


class WireSources(ContextSegment, declared_name=None):
    """Bus references belong to contributing segments, not their presentation route."""

    @classmethod
    def wire_sources(cls, original: SegmentManifest) -> tuple[MessageReference, ...]:
        return (
            *original.sources.bus_references(),
            *super().wire_sources(original),
        )


@dataclass(frozen=True, kw_only=True)
class UserInputSegment(SuppliedSegment, WireSources):
    def contribution(self, offset: int, text: str, images=()) -> InputContributionCoordinates:
        return replace(super().contribution(offset, text), images=tuple(range(len(images))))


@dataclass(frozen=True, kw_only=True)
class MeasuredNativeSegment(ContextSegment):
    """An SDK-owned input observation, not a second native message vocabulary."""

    tokens: int
    sha256: str
    utf8_bytes: int
    contributors: tuple[SegmentManifest, ...] = ()

    @abstractmethod
    def provider_value(self): ...

    @abstractmethod
    def render_into(self, prompt_parts, provider): ...

    def text(self):
        return FieldCodec.compact(self.provider_value(), ensure_ascii=False)

    def measured_manifest(self):
        # The SDK measures/hashes its exact original JSON representation.
        return SegmentManifest(kind=type(self), provenance=self.provenance,
                               sha256=self.sha256, utf8_bytes=self.utf8_bytes, tokens=self.tokens, contributors=self.contributors,
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


class SentOnce:
    """A kind whose value a Pi process sends once and names by hash from then on
    (``ContextSegment.publicationValues`` in stack/native): 500 KB of system prompt is
    not sent again with every request. Its text is the one that process sent."""

    @classmethod
    def sent_once(
        cls, texts: dict[tuple[type[ContextSegment], str], tuple[str, ...]],
        manifest: SegmentManifest,
    ) -> SegmentManifest:
        key = (cls, manifest.sha256)
        if manifest.captured_text:
            texts[key] = manifest.captured_text
        if key not in texts:
            raise ValueError(f"{cls.__name__} {manifest.sha256} was named before its process sent it")
        return replace(manifest, captured_text=texts[key])


@dataclass(frozen=True, kw_only=True)
class SystemLayerSegment(SentOnce, PublicInstructionAnnotations, MeasuredNativeSegment):
    content: str
    source_spans: Annotated[
        tuple[ContributionCoordinates, ...], WireOmitDefault()
    ] = ()

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
        return replace(provider, system_prompt=self.content)


@dataclass(frozen=True, kw_only=True)
class ToolCatalogSegment(SentOnce, MeasuredNativeSegment):
    tools: tuple[dict[str, JsonValue], ...]

    def provider_value(self):
        return list(self.tools)

    def public_spans(self) -> tuple[ContextSpan, ...]:
        offset = 1
        spans = []
        for tool in self.tools:
            text = FieldCodec.compact(tool, ensure_ascii=False)
            coordinates = ContributionCoordinates.capture(type(self), self.provenance, offset, text)
            spans.append(ContextSpan(self.sha256, coordinates))
            offset += coordinates.length + 1
        return tuple(spans)

    def render_into(self, prompt_parts, provider):
        return replace(provider, tools=self.tools)


@dataclass(frozen=True, kw_only=True)
class InstructionSegment(PublicInstructionAnnotations, ContextSegment):
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
    goal: tuple[str, ...]

    @classmethod
    def from_owner(cls, owner: ContextOwner, relation: PeerRelation, goals) -> PeerState:
        goal = relation.relevance.context_goal(owner.goal.active, NoGoal)
        return cls(
            owner.name,
            relation.relevance,
            relation.note,
            goal.instructions_for(goals, owner.incarnation),
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
    project: Checkout
    peers: tuple[PeerState, ...]

    @classmethod
    def capture(
        cls, owner: ContextOwner, peer_owners: tuple[ContextOwner, ...], relations: tuple[PeerRelation, ...], goals
    ) -> CoordinationSegment:
        response_instruction = WakePolicy.relevance_instruction()
        by_incarnation = {peer.incarnation: peer for peer in peer_owners}
        peers = tuple(PeerState.from_owner(by_incarnation[r.peer], r, goals)
                      for r in relations if r.peer in by_incarnation and r.relevance.grants(AlwaysListed))
        revision = hashlib.sha256(json.dumps(FieldCodec.encode(peers)).encode()).hexdigest()
        return super().capture(
            (OwnerProvenance(owner.incarnation, revision), response_instruction.source),
            response_instruction=response_instruction,
            owner=owner.incarnation,
            parent=owner.parent.otherwise(None),
            project=owner.worktree,
            peers=peers,
        )

    def values(self) -> dict[str, object]:
        return dict(
            response_instruction=self.response_instruction.content,
            name=self.owner.name,
            parent=self.parent,
            project=str(self.project.path),
            peers=FieldCodec.compact(FieldCodec.encode(self.peers)),
        )


@dataclass(frozen=True, kw_only=True)
class GoalSegment(InstructionSegment):
    goal: Goal
    orders_text: str = ""
    progress: str = ""

    @classmethod
    def capture(cls, owner: ContextOwner, goal: Goal, goals: GoalWorkSource) -> GoalSegment:
        todos = goal.order.todos(goals, owner.incarnation, goal.id)
        orders_text = goal.order.instructions(todos)
        progress = goal.order.progress(goal, todos)
        revision = hashlib.sha256(json.dumps(FieldCodec.encode((goal, orders_text, progress))).encode()).hexdigest()
        return super().capture((OwnerProvenance(owner.incarnation, revision),), goal=goal,
                               orders_text=orders_text, progress=progress)

    def values(self) -> dict[str, object]:
        return dict(goal_id=self.goal.id, goal_text=self.goal.render_instructions(self.orders_text), progress=self.progress)


@dataclass(frozen=True, kw_only=True)
class AutomaticTitleSegment(InstructionSegment):
    def values(self) -> dict[str, object]:
        return {}


@dataclass(frozen=True, kw_only=True)
class ReplyRouteSegment(InstructionSegment):
    targets: tuple[str, ...]

    def values(self) -> dict[str, object]:
        return dict(targets=", ".join(self.targets))


@dataclass(frozen=True, kw_only=True)
class ProviderContext:
    """Pi's provider context for one native turn, in the shape its provider request
    carries it: the system prompt, the tool catalog and the native messages. Each
    segment fills its own field; an unfilled field is absent from the wire."""

    system_prompt: Annotated[str, WireName("systemPrompt"), WireOmitDefault()] = ""
    tools: Annotated[tuple[dict[str, JsonValue], ...], WireOmitDefault()] = ()
    # Native messages accumulate across segments, so the record owns one list each
    # segment extends: one copy per message, never a recopy of the prefix.
    messages: Annotated[list[dict[str, JsonValue]], WireOmitDefault()] = field(
        default_factory=list)


@dataclass(frozen=True)
class RenderedInput:
    text: str
    provider: ProviderContext
    contributions: tuple[InputContributionCoordinates, ...] = ()


@dataclass(frozen=True)
class ContextTurn(DeclaredFamily, affix="ContextTurn"):
    @abstractmethod
    def provenance(self, owner: ContextOwner) -> Provenance: ...

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

    def provenance(self, owner: ContextOwner) -> Provenance:
        return TurnProvenance(owner.incarnation, self.identity)

    def require_recorded(self) -> RecordedContextTurn:
        return self

    def matches_generation(self, generation: int) -> bool:
        return self.occurrence.generation == generation


class NextContextTurn(ContextTurn):
    def provenance(self, owner: ContextOwner) -> Provenance:
        return OwnerProvenance(
            owner.incarnation,
            hashlib.sha256(json.dumps(owner.to_wire(), sort_keys=True).encode()).hexdigest(),
        )


class DispatchRequest(SubclassUnion, affix="DispatchRequest"):
    """The original request a captured context answered, or its unobserved dispatch.

    It permits neither new input nor replay. A recorded context is selected by its member as
    stored, so a capture whose dispatch was never observed is read like any other.
    """


@dataclass(frozen=True)
class UnobservedDispatchRequest(NullMember, DispatchRequest):
    """Previews and captures that never observed dispatch: Pi omits requestId."""


@dataclass(frozen=True)
class ObservedDispatchRequest(SingleFieldValue, DispatchRequest):
    request_id: PiRequestKey


@dataclass(frozen=True)
class ContextManifest:
    thread: ThreadIncarnation
    turn: ContextTurn
    segments: tuple[SegmentManifest, ...]
    counter: str
    request_id: Annotated[DispatchRequest, WireOmitDefault()] = field(
        default_factory=UnobservedDispatchRequest)

    @property
    def public_text_recorded(self) -> bool:
        return bool(self.segments) and all(segment.public_text_recorded for segment in self.segments)


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
    def for_request(
        cls, history, turn: RecordedContextTurn, request: DispatchRequest
    ) -> ContextManifest:
        """Select the original indexed publication, never today's preview."""
        selected = tuple(record for record in history
                         if record.turn == turn and record.request_id == request)
        if len(selected) != 1:
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


class ContextRecording(ABC):
    """The original previous context supplies its recording sequence and segments."""

    @abstractmethod
    def next_recording_turn(self) -> int: ...

    @abstractmethod
    def segment(self, position: int, original: SegmentManifest) -> SegmentRecording: ...


@dataclass(frozen=True)
class RecordedContextIdentity:
    """The request identity shared by storage and restored context records."""

    thread: ThreadIncarnation
    turn: ContextTurn
    counter: str
    request_id: Annotated[DispatchRequest, WireOmitDefault()] = field(
        default_factory=UnobservedDispatchRequest)
    recording_turn: Annotated[int, WireOmitDefault()] = 1

    def with_segments(
        self, segments: tuple[RecordedSegmentManifest, ...],
    ) -> RecordedContextManifest:
        return RecordedContextManifest(self.thread, self.turn, self.counter, segments=segments,
            request_id=self.request_id, recording_turn=self.recording_turn)


@dataclass(frozen=True)
class RecordedContextManifest(RecordedContextIdentity):
    """One turn's context identity and references to separately recorded entries."""

    segments: Annotated[
        tuple[RecordedSegmentManifest, ...], IdentityExclude()
    ] = field(kw_only=True)

    @classmethod
    def capture(
        cls, manifest: ContextManifest,
        previous: ContextRecording,
    ) -> tuple[RecordedContextManifest, tuple[ContextEntryManifest, ...]]:
        recording_turn = previous.next_recording_turn()
        parts = []
        for position, segment in enumerate(manifest.segments):
            parts.append(segment.kind.record_manifest(
                segment, previous.segment(position, segment), recording_turn=recording_turn))
        return (
            cls(
                thread=manifest.thread,
                turn=manifest.turn,
                segments=tuple(part for part, _ in parts),
                counter=manifest.counter,
                request_id=manifest.request_id,
                recording_turn=recording_turn,
            ),
            tuple(entry for _, group in parts for entry in group),
        )

    def restore(self, entries: dict[ContextEntryReference, SegmentManifest],
                previous: Mapping[int, RecordedContextManifest] = _NO_CONTEXT_RECORDS,
                ) -> ContextManifest:
        previous = {} if previous is None else previous
        return ContextManifest(
            thread=self.thread,
            turn=self.turn,
            segments=tuple(segment.restore(entries, previous, position)
                  for position, segment in enumerate(self.segments)),
            counter=self.counter,
            request_id=self.request_id,
        )


@dataclass(frozen=True)
class TurnContext(DeclaredReply, GoalContextSource[ContextOwner, GoalWorkSource, "TurnContext"]):
    thread: ThreadIncarnation
    turn: ContextTurn
    segments: tuple[ContextSegment, ...]

    def with_goal(self, owner: ContextOwner, goals: GoalWorkSource) -> TurnContext:
        return owner.goal.active().add_context(
            self, partial(self.prepend_goal, owner, goals=goals),
        )

    def prepend_goal(self, owner: ContextOwner, goal: Goal, goals: GoalWorkSource) -> TurnContext:
        return self.prepend(GoalSegment.capture(owner, goal, goals))

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
        owner: ContextOwner,
        turn: ContextTurn,
        task: str,
        peer_owners: tuple[ContextOwner, ...],
        relations: tuple[PeerRelation, ...],
        goals,
        origins: tuple[MessageReference, ...] = (),
        inputs: tuple[ContextProvenanceSource[Provenance], ...] = (),
    ) -> TurnContext:
        source = tuple(WireProvenance(ref) for ref in origins) or (turn.provenance(owner),)
        source += tuple(row.context_provenance() for row in inputs)
        return cls(
            owner.incarnation,
            turn,
            (
                CoordinationSegment.capture(owner, peer_owners, relations, goals),
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
        prompt_parts: list[str] = []
        provider = ProviderContext()
        contributions: list[InputContributionCoordinates] = []
        offset = 0
        for segment in segments:
            start = len(prompt_parts)
            provider = segment.render_into(prompt_parts, provider)
            if len(prompt_parts) > start:
                contribution = segment.contribution(offset, "".join(prompt_parts[start:]), images)
                contributions.append(contribution)
                offset += contribution.length
        return RenderedInput("".join(prompt_parts), provider, tuple(contributions))
