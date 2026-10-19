"""Pure claim transitions for the single-authority bus envelope.

No bus send, file append, O_EXCL claim sidecar, write fence, or retry lives here.
A caller must serialize send and every claim-bearing read under the wire→bus
locks, accept only newline-complete rows after fsync of the opened bus file (and
its parent on creation), and quarantine/re-fsync a visible row after failed
fsync before any reader observes it. Parsed rows alone are NOT durable proof.
"""

from __future__ import annotations

import stat
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, ClassVar, Generic, Literal, TypeGuard, TypeVar

from .coordination_errors import IdentityConflict
from .field_codec import (
    BoundedText,
    EnvelopeSequence,
    FieldCodec,
    Id128Text,
    ProjectionSequence,
    RecordMember,
    ResourcePathText,
    Sha256Text,
    SubclassUnion,
)
from .message_reference import MessageReference
from .thread_identity import GenerationCounter, RecordedThreadIncarnation, TurnId

if TYPE_CHECKING:
    from .coordination_tables.attempts import AttemptRecord
    from .registry_document import RegistrySnapshot


class ClaimTransitionError(ValueError):
    """An invalid request or unverified/corrupt projection input."""


class ClaimConflict(ClaimTransitionError):  # noqa: N818 - domain-specific losing result
    """A synchronous losing transition; never queue or retry it automatically."""

    def __init__(self, existing: ClaimOwner):
        self.existing = existing
        super().__init__(f"Resource already claimed by {existing.owner!r}")


@dataclass(frozen=True)
class FileClaimPath(ABC):
    """A caller's file operation owns which physical paths may be claimed."""

    resource: Path

    def __post_init__(self) -> None:
        if not isinstance(self.resource, Path):
            raise TypeError("File claim requires a parsed path")

    def normalized(self, root: Path) -> str:
        base = Path(root).absolute()
        try:
            physical = base.resolve(strict=True) == base and base.is_dir()
        except (OSError, RuntimeError) as error:
            raise ClaimTransitionError("Worktree must be a physical existing directory.") from error
        if not physical:
            raise ClaimTransitionError("Worktree must be a physical existing directory.")
        requested = self.resource
        if ".." in requested.parts:
            raise ClaimTransitionError("Parent traversal is not a resource identifier.")
        candidate = requested if requested.is_absolute() else base / requested
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as error:
            raise ClaimTransitionError(f"Cannot resolve file path {candidate}: {error}") from error
        if resolved != candidate:
            raise ClaimTransitionError("Symlink aliases are not supported.")
        if resolved == base:
            raise ClaimTransitionError("Worktree directories cannot be claimed.")
        try:
            self.validate_leaf(resolved)
        except FileNotFoundError as error:
            raise ClaimTransitionError(f"File does not exist: {resolved}") from error
        except OSError as error:
            raise ClaimTransitionError(f"Cannot inspect file {resolved}: {error}") from error
        return str(resolved)

    @abstractmethod
    def validate_leaf(self, path: Path) -> None:
        """Validate the file state required by this operation."""

    @staticmethod
    def validate_regular(info) -> None:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ClaimTransitionError("Only regular singly-linked files can be claimed.")


class ExistingFileClaim(FileClaimPath):
    def validate_leaf(self, path: Path) -> None:
        self.validate_regular(path.stat())


class WritableFileClaim(FileClaimPath):
    """Write may create a missing file, including missing parent directories."""

    def validate_leaf(self, path: Path) -> None:
        try:
            info = path.stat()
        except FileNotFoundError:
            return
        self.validate_regular(info)


@dataclass(frozen=True, slots=True)
class AssignmentBinding:
    """The selected claim's revision and source, used for permission checks and its SQL row."""

    assignment_id: Annotated[str, BoundedText]
    revision: int
    recipient_lookup: Annotated[str, BoundedText]
    source: MessageReference
    execution_id: Annotated[str, BoundedText]

    def __post_init__(self) -> None:
        FieldCodec.require_fields(self)
        if ":" in self.execution_id:
            raise ClaimTransitionError("Execution ID cannot contain a colon.")
        if FieldCodec.decode(int, self.source.seq) <= 0:
            raise ClaimTransitionError("Assignment source sequence must be positive.")
        if FieldCodec.decode(int, self.revision) <= 0:
            raise ClaimTransitionError("Assignment revision must be positive.")


@dataclass(frozen=True, slots=True)
class WakeClaim:
    """A selected N/K execution bound into the durable resource-claim row."""

    wire_root_id: str
    source_seq: int
    source_message_id: Annotated[str, BoundedText]
    wake_assignment_id: Annotated[str, BoundedText] = field(metadata={"wire_name": "wake_claim_id"})
    wake_revision: int
    recipient_lookup: str
    execution_id: Annotated[str, BoundedText]
    operation_id: Annotated[str, Id128Text]
    owner_claimed_generation: int = field(metadata={"wire_name": "owner_admission_generation"})
    turn_id: str
    participant_generation: int
    attempt_ordinal: int
    version: int = field(default=1, metadata={"wire_required": True})

    @property
    def binding(self) -> AssignmentBinding:
        return AssignmentBinding(
            self.wake_assignment_id,
            self.wake_revision,
            self.recipient_lookup,
            self.source,
            self.execution_id,
        )

    @property
    def source(self) -> MessageReference:
        return MessageReference(self.source_seq, self.source_message_id)

    def __post_init__(self) -> None:
        Id128Text.decode(self.wire_root_id)
        Id128Text.decode(self.recipient_lookup)
        try:
            FieldCodec.decode(Literal[1], self.version)
            AssignmentBinding(
                self.wake_assignment_id,
                self.wake_revision,
                self.recipient_lookup,
                self.source,
                self.execution_id,
            )
            GenerationCounter.require_positive(self.owner_claimed_generation)
            GenerationCounter.require_positive(self.participant_generation)
            if FieldCodec.decode(int, self.attempt_ordinal) <= 0:
                raise ValueError("attempt ordinal must be positive")
        except ValueError as error:
            raise ClaimTransitionError(
                "Wake claim_scope version or revision is invalid."
            ) from error
        TurnId.for_registration(self.turn_id)
        try:
            assignment = FieldCodec.decode(str, self.wake_assignment_id)
            if not assignment.startswith("cohort-v1:"):
                raise ValueError("Unknown wake claim codec")
            Sha256Text.from_text(assignment.removeprefix("cohort-v1:"))
        except ValueError as error:
            raise ClaimTransitionError("Wake claim ID is invalid.") from error


@dataclass(frozen=True, slots=True)
class ClaimRelease:
    resource: Annotated[str, ResourcePathText]
    generation: Annotated[str, Id128Text]

    def __post_init__(self) -> None:
        FieldCodec.require_fields(self)


Claimant = TypeVar("Claimant", bound="ClaimOwner")


@dataclass(frozen=True)
class ClaimTransition(SubclassUnion, ABC, affix=""):
    """One original resource transition; its member declares what it changes."""

    max_resources: ClassVar[int] = 32
    owner: Annotated[str, BoundedText]
    incarnation: Annotated[str, BoundedText]
    seq: Annotated[int, EnvelopeSequence]
    message_id: Annotated[str, BoundedText]

    def __post_init__(self) -> None:
        FieldCodec.require_fields(self)
        if not self.resources or len(self.resources) > self.max_resources:
            raise ClaimTransitionError("Claim transitions need a bounded resource set.")
        if len(set(self.resources)) != len(self.resources):
            raise ClaimTransitionError("Duplicate or overlapping resources are not supported.")

    @property
    @abstractmethod
    def resources(self) -> tuple[str, ...]: ...

    def require_new_operation(self, is_used: Callable[[str], bool]) -> None:
        """Release and manual originals consume no selected operation."""

    @abstractmethod
    def claimed(self, resource: str) -> ClaimOwner: ...

    @abstractmethod
    def apply(self, previous: ClaimProjection, registry: RegistrySnapshot) -> ClaimProjection: ...

    @abstractmethod
    def project(self, previous: ClaimProjection) -> ClaimProjection: ...

    def remaining(
        self, previous: ClaimProjection, releases: tuple[ClaimRelease, ...]
    ) -> dict[str, ClaimOwner]:
        if self.seq <= previous.last_seq:
            raise ClaimTransitionError("Claim sequence must increase.")
        remaining = dict(previous)
        for release in releases:
            current = remaining.get(release.resource)
            if current is None:
                raise ClaimTransitionError("Cannot release a resource without a live claim.")
            if (current.incarnation, current.generation) != (self.incarnation, release.generation):
                raise ClaimTransitionError(
                    "Only the exact owner incarnation/generation may release."
                )
            del remaining[release.resource]
        return remaining


@dataclass(frozen=True)
class ClaimAcquisition(ClaimTransition, Generic[Claimant], ABC):
    claims: tuple[Annotated[str, ResourcePathText], ...]
    releases: tuple[ClaimRelease, ...]
    generation: Annotated[str, Id128Text]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.claims:
            raise ClaimTransitionError("Acquisition requires resources.")

    @property
    def resources(self) -> tuple[str, ...]:
        return (*self.claims, *(release.resource for release in self.releases))

    def owner_arguments(self) -> tuple[str, str, str, int, str]:
        return self.owner, self.incarnation, self.generation, self.seq, self.message_id

    def claimed(self, resource: str) -> Claimant:
        if resource not in self.claims:
            raise ClaimTransitionError("Resource is not part of this acquisition.")
        return self.owner_for(resource)

    @abstractmethod
    def owner_for(self, resource: str) -> Claimant: ...

    def apply(self, previous: ClaimProjection, registry: RegistrySnapshot) -> ClaimProjection:
        for resource in self.claims:
            if resource in previous and previous[resource].live(registry):
                raise ClaimConflict(previous[resource])
        return self.project(previous)

    def project(self, previous: ClaimProjection) -> ClaimProjection:
        remaining = self.remaining(previous, self.releases)
        for resource in self.claims:
            remaining[resource] = self.claimed(resource)
        return ClaimProjection(self.seq, remaining)


@dataclass(frozen=True)
class ManualClaims(RecordMember, ClaimAcquisition["ClaimOwner"]):
    """Already committed manual originals remain readable, never a writer input."""

    def owner_for(self, resource: str) -> ClaimOwner:
        return ManualClaimOwner(resource, *self.owner_arguments())


@dataclass(frozen=True)
class WakeClaims(RecordMember, ClaimAcquisition["WakeClaimOwner"]):
    claim_scope: WakeClaim = field(metadata={"wire_name": "admission"})

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.releases:
            raise ClaimTransitionError("Selected acquisitions cannot release resources.")

    def owner_for(self, resource: str) -> WakeClaimOwner:
        return WakeClaimOwner(resource, *self.owner_arguments(), self.claim_scope)

    def require_new_operation(self, is_used: Callable[[str], bool]) -> None:
        if is_used(self.claim_scope.operation_id):
            raise IdentityConflict("Wake claim operation was already consumed")


@dataclass(frozen=True)
class ReleasedClaims(RecordMember, ClaimTransition):
    releases: tuple[ClaimRelease, ...]
    claims: tuple[()] = ()
    generation: Literal[None] = None

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(release.resource for release in self.releases)

    def claimed(self, resource: str) -> ClaimOwner:
        raise ClaimTransitionError("Release originals do not acquire resources.")

    def apply(self, previous: ClaimProjection, registry: RegistrySnapshot) -> ClaimProjection:
        return self.project(previous)

    def project(self, previous: ClaimProjection) -> ClaimProjection:
        return ClaimProjection(self.seq, self.remaining(previous, self.releases))


@dataclass(frozen=True)
class ClaimOwner(SubclassUnion, ABC, affix=""):
    resource: Annotated[str, ResourcePathText]
    owner: Annotated[str, BoundedText]
    incarnation: Annotated[str, BoundedText]
    generation: Annotated[str, Id128Text]
    seq: Annotated[int, EnvelopeSequence]
    message_id: Annotated[str, BoundedText]

    def __post_init__(self) -> None:
        FieldCodec.require_fields(self)

    @abstractmethod
    def live(self, registry: RegistrySnapshot) -> bool: ...

    @abstractmethod
    def belongs_to(self, attempt: AttemptRecord) -> bool: ...


@dataclass(frozen=True)
class ManualClaimOwner(RecordMember, ClaimOwner):
    def live(self, registry: RegistrySnapshot) -> bool:
        """Unscoped historical originals grant no selected execution permission."""
        return False

    def belongs_to(self, attempt: AttemptRecord) -> bool:
        return False


@dataclass(frozen=True)
class WakeClaimOwner(RecordMember, ClaimOwner):
    claim_scope: WakeClaim = field(metadata={"wire_name": "admission"})

    def live(self, registry: RegistrySnapshot) -> bool:
        incarnation = RecordedThreadIncarnation(self.owner, float(self.incarnation))
        return any(
            registry.status(thread.name).running and thread.process_alive
            and registry.claimed_generations[thread.name] == self.claim_scope.owner_claimed_generation
            for thread in registry.threads_for(incarnation)
        )

    @classmethod
    def selected(cls, owner: ClaimOwner, scope: WakeClaim) -> TypeGuard[WakeClaimOwner]:
        return isinstance(owner, cls) and owner.claim_scope == scope

    def belongs_to(self, attempt: AttemptRecord) -> bool:
        scope = self.claim_scope
        return (
            scope.execution_id == attempt.execution_id
            and scope.attempt_ordinal == attempt.attempt_ordinal
            and scope.recipient_lookup == attempt.owner_lookup
            and scope.participant_generation == attempt.owner_generation
        )


@dataclass(frozen=True, slots=True)
class ClaimProjection(Mapping[str, ClaimOwner]):
    """Immutable sorted claim table plus the last processed envelope sequence."""

    last_seq: Annotated[int, ProjectionSequence] = 0
    # Built from a dict, which the codec checks; held afterwards as a read-only view.
    _claims: dict[str, ClaimOwner] = field(default_factory=dict)

    def __post_init__(self) -> None:
        FieldCodec.require_fields(self)
        if any(
            value.resource != key or value.seq > self.last_seq
            for key, value in self._claims.items()
        ):
            raise ClaimTransitionError("Previous projection is malformed.")
        object.__setattr__(self, "_claims", MappingProxyType(dict(sorted(self._claims.items()))))

    def __getitem__(self, key: str) -> ClaimOwner:
        return self._claims[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._claims)

    def __len__(self) -> int:
        return len(self._claims)
