"""Original turn contributors and their observations, never input authority."""

from __future__ import annotations

import hashlib
import json
from abc import abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .message_reference import MessageReference
from .goals import Goal
from .thread_identity import ThreadIncarnation, TurnId

if TYPE_CHECKING:
    from .presentation import ThreadView
    from .threads import Thread


@dataclass(frozen=True)
class Provenance(DeclaredFamily, affix="Provenance"):
    """A source coordinate, not permission to execute or replay it."""


@dataclass(frozen=True)
class FileProvenance(Provenance):
    path: str
    sha256: str


@dataclass(frozen=True)
class OwnerProvenance(Provenance):
    owner: ThreadIncarnation
    revision: str


@dataclass(frozen=True)
class WireProvenance(Provenance):
    source: MessageReference


@dataclass(frozen=True)
class JournalProvenance(Provenance):
    path: str
    entries: tuple[str, ...]


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
    kind: str
    provenance: tuple[Provenance, ...]
    sha256: str
    utf8_bytes: int
    tokens: int


@dataclass(frozen=True, kw_only=True)
class ContextSegment(DeclaredFamily, affix="Segment"):
    provenance: tuple[Provenance, ...]

    def __post_init__(self) -> None:
        if not self.provenance:
            raise TypeError("A context contribution requires its original source")

    @abstractmethod
    def text(self) -> str: ...

    def manifest(self, tokens: int) -> SegmentManifest:
        raw = self.text().encode()
        return SegmentManifest(
            self.declared_name, self.provenance, hashlib.sha256(raw).hexdigest(), len(raw), tokens
        )


@dataclass(frozen=True, kw_only=True)
class SuppliedSegment:
    """Content supplied by its owner at a decoded external boundary."""

    content: str

    def text(self) -> str:
        return self.content


@dataclass(frozen=True, kw_only=True)
class TranscriptSegment(SuppliedSegment, ContextSegment):
    pass


@dataclass(frozen=True, kw_only=True)
class CompactionSummarySegment(SuppliedSegment, ContextSegment):
    pass


@dataclass(frozen=True, kw_only=True)
class InjectionSegment(SuppliedSegment, ContextSegment):
    pass


@dataclass(frozen=True, kw_only=True)
class SystemLayerSegment(SuppliedSegment, ContextSegment):
    pass


@dataclass(frozen=True, kw_only=True)
class InstructionSegment(ContextSegment):
    instruction: InstructionFile

    @abstractmethod
    def values(self) -> dict[str, object]: ...

    def text(self) -> str:
        return self.instruction.render(self.values())


@dataclass(frozen=True)
class PeerState:
    """A projection of original roster declarations, not copied presence state."""

    name: str
    status: str
    activity: str
    activity_detail: str

    @classmethod
    def from_view(cls, view: ThreadView) -> PeerState:
        return cls(
            view.thread.name,
            view.status.declared_name,
            view.activity.state.value,
            view.activity.detail,
        )


@dataclass(frozen=True, kw_only=True)
class CoordinationSegment(InstructionSegment):
    owner: ThreadIncarnation
    parent: str | None
    project: str
    peers: tuple[PeerState, ...]

    @classmethod
    def capture(cls, owner: Thread, views: tuple[ThreadView, ...]) -> CoordinationSegment:
        instruction = InstructionFile.read("coordination.md")
        peers = tuple(
            PeerState.from_view(view)
            for view in sorted(views, key=lambda v: v.thread.name)
            if view.thread.name != owner.name
        )[:50]
        revision = hashlib.sha256(json.dumps(FieldCodec.encode(peers)).encode()).hexdigest()
        return cls(
            provenance=(OwnerProvenance(owner.incarnation, revision), instruction.source),
            instruction=instruction,
            owner=owner.incarnation,
            parent=owner.parent,
            project=owner.worktree,
            peers=peers,
        )

    def values(self) -> dict[str, object]:
        return dict(
            name=self.owner.name,
            parent=self.parent,
            project=self.project,
            peers=json.dumps(FieldCodec.encode(self.peers)),
        )


@dataclass(frozen=True, kw_only=True)
class GoalSegment(InstructionSegment):
    goal: Goal

    @classmethod
    def capture(cls, owner: Thread) -> GoalSegment:
        goal = owner.goal
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


@dataclass(frozen=True)
class ContextTurn(DeclaredFamily, affix="ContextTurn"):
    @abstractmethod
    def source_revision(self, owner: Thread) -> str: ...


@dataclass(frozen=True)
class RecordedContextTurn(ContextTurn):
    identity: TurnId

    def source_revision(self, owner: Thread) -> str:
        return self.identity.value


class NextContextTurn(ContextTurn):
    def source_revision(self, owner: Thread) -> str:
        return hashlib.sha256(json.dumps(owner.to_wire(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ContextManifest:
    thread: ThreadIncarnation
    turn: ContextTurn
    segments: tuple[SegmentManifest, ...]
    counter: str


@dataclass(frozen=True)
class TurnContext:
    thread: ThreadIncarnation
    turn: ContextTurn
    segments: tuple[ContextSegment, ...]

    @classmethod
    def for_owner(
        cls,
        owner: Thread,
        turn: ContextTurn,
        task: str,
        views: tuple[ThreadView, ...],
        origins: tuple[MessageReference, ...] = (),
    ) -> TurnContext:
        source = tuple(WireProvenance(ref) for ref in origins) or (
            OwnerProvenance(owner.incarnation, turn.source_revision(owner)),
        )
        return cls(
            owner.incarnation,
            turn,
            (
                CoordinationSegment.capture(owner, views),
                TranscriptSegment(provenance=source, content=task),
            ),
        )

    def prepend(self, segment: ContextSegment) -> TurnContext:
        return replace(self, segments=(segment, *self.segments))

    def append(self, segment: ContextSegment) -> TurnContext:
        return replace(self, segments=(*self.segments, segment))

    def render(self) -> RenderedInput:
        return RenderedInput("".join(segment.text() for segment in self.segments))

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
