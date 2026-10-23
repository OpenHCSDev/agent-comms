"""Pure resource transitions; Bus owns their atomic publication and projection."""

from __future__ import annotations

import stat
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal


from .coordination_errors import IdentityConflict
from .field_codec import (
    BoundedText,
    EnvelopeSequence,
    FieldCodec,
    Id128Text,
    NullMember,
    ProjectionSequence,
    RecordMember,
    ResourcePathText,
    Sha256Text,
    SubclassUnion,
    WireName,
    WireOmitDefault,
    WireRequired,
)
from .message_reference import MessageReference
from .thread_identity import GenerationCounter, RecordedThreadIncarnation, TurnId

if TYPE_CHECKING:
    from .coordination_tables.attempts import AttemptRecord
    from .registry_document import RegistrySnapshot

if TYPE_CHECKING:
    pass


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


@dataclass(frozen=True)
class ClaimScope(SubclassUnion, ABC, affix=""):
    """What one acquisition belongs to; its member answers reuse, liveness and operation use."""

    def reuses(self, other: RecordedScope) -> bool:
        return self == other

    def belongs_to(self, attempt: AttemptRecord) -> bool:
        return False

    @abstractmethod
    def held_at(self, claimed_generation: int) -> bool:
        """Whether the owner's current claimed generation still holds this acquisition."""

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class UnrecordedScope(NullMember, ClaimScope):
    """Bus history written before claims recorded a scope: the scope key is null or absent.

    Nothing ties such a holder to a current input, so it is never live, and it
    consumed no operation.
    """

    def held_at(self, claimed_generation: int) -> bool:
        return False


@dataclass(frozen=True)
class RecordedScope(ClaimScope, declared_name=None):
    """Every acquisition published today records the input generation and operation it serves."""

    owner_claimed_generation: Annotated[
        int, WireName("owner_admission_generation")
    ]
    operation_id: Annotated[str, Id128Text]
    turn_id: str

    def __post_init__(self) -> None:
        FieldCodec.require_fields(self)
        GenerationCounter.require_positive(self.owner_claimed_generation)
        TurnId.for_registration(self.turn_id)

    def held_at(self, claimed_generation: int) -> bool:
        return claimed_generation == self.owner_claimed_generation

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return (self.operation_id,)


@dataclass(frozen=True)
class NativeClaim(RecordMember, RecordedScope):
    """A tool acquisition belongs to one ordinary native input."""

    input_id: Annotated[str, BoundedText]

    def reuses(self, other: RecordedScope) -> bool:
        return replace(self, operation_id=other.operation_id) == other


@dataclass(frozen=True)
class WakeClaim(RecordMember, RecordedScope):
    """A selected N/K execution bound into the durable resource-claim row."""

    wire_root_id: Annotated[str, Id128Text]
    source_seq: int
    source_message_id: Annotated[str, BoundedText]
    wake_assignment_id: Annotated[str, BoundedText, WireName("wake_claim_id")]
    wake_revision: int
    recipient_lookup: Annotated[str, Id128Text]
    execution_id: Annotated[str, BoundedText]
    participant_generation: int
    attempt_ordinal: int
    version: Annotated[Literal[1], WireRequired()] = 1

    def belongs_to(self, attempt: AttemptRecord) -> bool:
        # The attempts table's primary key fixes its immutable participant identity.
        return (
            self.execution_id == attempt.execution_id
            and self.attempt_ordinal == attempt.attempt_ordinal
        )

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
        super().__post_init__()
        try:
            self.binding
            GenerationCounter.require_positive(self.participant_generation)
            if FieldCodec.decode(int, self.attempt_ordinal) <= 0:
                raise ValueError("attempt ordinal must be positive")
        except ValueError as error:
            raise ClaimTransitionError(
                "Wake claim_scope version or revision is invalid."
            ) from error
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


@dataclass(frozen=True)
class ClaimIdentity:
    owner: Annotated[str, BoundedText]
    incarnation: Annotated[str, BoundedText]
    seq: Annotated[int, EnvelopeSequence]
    message_id: Annotated[str, BoundedText]


@dataclass(frozen=True)
class AcquiringClaim(ClaimIdentity):
    generation: Annotated[str, Id128Text]
    resource_scope: Annotated[ClaimScope, WireName("admission")]


@dataclass(frozen=True)
class ClaimTransition(ClaimIdentity, SubclassUnion, ABC, affix=""):
    """One original resource transition; its member declares what it changes."""

    max_resources: ClassVar[int] = 32
    expired: Annotated[
        tuple[Annotated[str, ResourcePathText], ...], WireOmitDefault()
    ] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        FieldCodec.require_fields(self)
        if not self.resources or len(self.resources) > self.max_resources:
            raise ClaimTransitionError("Claim transitions need a bounded resource set.")
        if len(set(self.resources)) != len(self.resources):
            raise ClaimTransitionError("Duplicate or overlapping resources are not supported.")

    @property
    @abstractmethod
    def resources(self) -> tuple[str, ...]: ...

    @property
    def operation_ids(self) -> tuple[str, ...]:
        """A release consumes no acquisition operation."""
        return ()

    def require_new_operation(self, is_used: Callable[[str], bool]) -> None:
        for operation_id in self.operation_ids:
            if is_used(operation_id):
                raise IdentityConflict("Claim operation was already consumed")

    def claimed(self, resource: str) -> ClaimOwner:
        raise ClaimTransitionError("Release originals do not acquire resources.")

    def apply(self, previous: ClaimProjection, registry: RegistrySnapshot) -> ClaimProjection:
        live = previous.live_in(registry)
        self.require_holders(live)
        # Only registry evidence expires original claims; never accept caller-authored expiry.
        return replace(self, expired=()).project(live)

    def require_holders(self, previous: ClaimProjection) -> None:
        """Release members validate their exact generation while projecting."""

    @abstractmethod
    def project(self, previous: ClaimProjection) -> ClaimProjection: ...

    def remaining(
        self, previous: ClaimProjection, releases: tuple[ClaimRelease, ...]
    ) -> dict[str, ClaimOwner]:
        if self.seq <= previous.last_seq:
            raise ClaimTransitionError("Claim sequence must increase.")
        remaining = {resource: owner for resource, owner in previous.items()
                     if resource not in self.expired}
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
class Acquisition(ClaimTransition, declared_name=None):
    """A transition that acquires its resources under one fresh generation."""

    claims: tuple[Annotated[str, ResourcePathText], ...] = field(kw_only=True)
    # The stored bus row carries both sides of a transition; an acquisition's release side is empty.
    releases: Annotated[tuple[ClaimRelease, ...], WireRequired()] = field(
        default=(), kw_only=True
    )
    generation: Annotated[str, Id128Text] = field(kw_only=True)
    resource_scope: ClassVar[ClaimScope]

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.releases:
            raise ClaimTransitionError("An acquisition row releases nothing.")

    @property
    def resources(self) -> tuple[str, ...]:
        return self.claims

    def claimed(self, resource: str) -> ClaimOwner:
        if resource not in self.claims:
            raise ClaimTransitionError("Resource is not part of this acquisition.")
        return ClaimOwner(
            resource=resource,
            **{declared.name: getattr(self, declared.name) for declared in fields(AcquiringClaim)},
        )

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return self.resource_scope.operation_ids

    def require_holders(self, previous: ClaimProjection) -> None:
        for resource in self.claims:
            if resource in previous:
                previous[resource].require_holder(self.owner, self.incarnation)

    def project(self, previous: ClaimProjection) -> ClaimProjection:
        remaining = self.remaining(previous, ())
        for resource in self.claims:
            remaining[resource] = self.claimed(resource)
        return ClaimProjection(self.seq, remaining)


@dataclass(frozen=True)
class WakeClaims(RecordMember, Acquisition, AcquiringClaim):
    """An acquisition published today; its row records the scope it serves."""

    resource_scope: Annotated[RecordedScope, WireName("admission")]


@dataclass(frozen=True)
class UnscopedClaims(RecordMember, Acquisition):
    """An ordinary turn's tool acquisition written before claims recorded a scope.

    Only bus history holds this row; no writer publishes one.
    """

    resource_scope: ClassVar[ClaimScope] = UnrecordedScope()


@dataclass(frozen=True)
class ReleasedClaims(RecordMember, ClaimTransition):
    releases: tuple[ClaimRelease, ...]
    # The stored bus row carries both sides of a transition; a release acquires nothing and mints no generation.
    claims: Annotated[
        tuple[Annotated[str, ResourcePathText], ...], WireRequired()
    ] = ()
    generation: Annotated[None, WireRequired()] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.claims:
            raise ClaimTransitionError("A release row acquires nothing.")

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(release.resource for release in self.releases)

    def project(self, previous: ClaimProjection) -> ClaimProjection:
        return ClaimProjection(self.seq, self.remaining(previous, self.releases))


class ResourceClaim:
    def acquire(
        self, scope: RecordedScope, registry: RegistrySnapshot, publish: Callable[[], ClaimOwner]
    ) -> ClaimOwner:
        return publish()


@dataclass(frozen=True)
class UnclaimedResource(ResourceClaim):
    """A checked resource has not been acquired."""


@dataclass(frozen=True)
class ClaimOwner(AcquiringClaim, ResourceClaim):
    resource: Annotated[str, ResourcePathText]

    def __post_init__(self) -> None:
        FieldCodec.require_fields(self)

    def acquire(
        self, scope: RecordedScope, registry: RegistrySnapshot, publish: Callable[[], ClaimOwner]
    ) -> ClaimOwner:
        return self if self.resource_scope.reuses(scope) and self.live(registry) else publish()

    def require_holder(self, owner: str, incarnation: str) -> None:
        if (self.owner, self.incarnation) != (owner, incarnation):
            raise ClaimConflict(self)

    def live(self, registry: RegistrySnapshot) -> bool:
        incarnation = RecordedThreadIncarnation(self.owner, float(self.incarnation))
        return any(
            thread.process_alive
            and self.resource_scope.held_at(registry.claim_generation(thread.name))
            for thread in registry.active_threads(registry.threads_for(incarnation))
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

    def live_in(self, registry: RegistrySnapshot) -> ClaimProjection:
        """Keep only claims whose recorded owner still holds its registry generation."""
        return ClaimProjection(self.last_seq, {
            resource: owner for resource, owner in self.items() if owner.live(registry)
        })

    def __getitem__(self, key: str) -> ClaimOwner:
        return self._claims[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._claims)

    def __len__(self) -> int:
        return len(self._claims)
