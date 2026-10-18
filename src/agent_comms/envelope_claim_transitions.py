"""Pure claim transitions for the single-authority bus envelope.

No bus send, file append, O_EXCL claim sidecar, write fence, or retry lives here.
A caller must serialize send and every claim-bearing read under the wire→bus
locks, accept only newline-complete rows after fsync of the opened bus file (and
its parent on creation), and quarantine/re-fsync a visible row after failed
fsync before any reader observes it. Parsed rows alone are NOT durable proof.
"""

from __future__ import annotations

import os
import stat
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Literal

from .coordination_errors import IdentityConflict
from .errors import RelationViolationError
from .message_reference import MessageReference
from .field_codec import FieldCodec, Id128Text, Sha256Text
from .thread_identity import GenerationCounter, TurnId


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


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > 4096:
        raise ClaimTransitionError(f"{label} must be a bounded nonempty string.")
    return value


def _generation(value: object) -> str:
    try:
        return Id128Text.decode(value)
    except ValueError as error:
        raise ClaimTransitionError("Generation must be a lowercase 128-bit hex value.") from error


def _resource(value: object) -> str:
    path = _text(value, "Resource")
    if not Path(path).is_absolute() or os.path.normpath(path) != path:
        raise ClaimTransitionError("Resource must already be a canonical absolute path.")
    return path


@dataclass(frozen=True, slots=True)
class AssignmentBinding:
    """The selected claim's revision and source, used for permission checks and its SQL row."""

    assignment_id: str
    revision: int
    recipient_lookup: str
    source: MessageReference
    execution_id: str

    def __post_init__(self) -> None:
        _text(self.assignment_id, "Assignment ID")
        _text(self.recipient_lookup, "Recipient lookup")
        _text(self.source.message_id, "Source message ID")
        _text(self.execution_id, "Execution ID")
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
    source_message_id: str
    wake_assignment_id: str = field(metadata={"wire_name": "wake_claim_id"})
    wake_revision: int
    recipient_lookup: str
    execution_id: str
    operation_id: str
    owner_claimed_generation: int = field(metadata={"wire_name": 'owner_admission_generation'})
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
        _generation(self.operation_id)
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
            raise ClaimTransitionError("Wake claim_scope version or revision is invalid.") from error
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
    resource: str
    generation: str

    def __post_init__(self) -> None:
        _resource(self.resource)
        _generation(self.generation)


@dataclass(frozen=True, slots=True)
class ClaimTransition:
    """Claims and releases in ONE future message row, never independent writes."""

    max_resources: ClassVar[int] = 32

    owner: str
    incarnation: str
    seq: int
    message_id: str
    claims: tuple[str, ...] = field(default=(), metadata={"wire_required": True})
    releases: tuple[ClaimRelease, ...] = field(default=(), metadata={"wire_required": True})
    generation: str | None = field(default=None, metadata={"wire_required": True})
    claim_scope: WakeClaim | None = field(
        default=None, metadata={"wire_name": 'admission', "wire_omit_default": True, "wire_nonnull": True}
    )

    def __post_init__(self) -> None:
        _text(self.owner, "Owner")
        _text(self.incarnation, "Incarnation")
        _text(self.message_id, "Message ID")
        if type(self.seq) is not int or self.seq <= 0:
            raise ClaimTransitionError("Sequence must be a positive exact integer.")
        if type(self.claims) is not tuple or type(self.releases) is not tuple:
            raise ClaimTransitionError("Claims and releases must be tuples.")
        if not self.claims and not self.releases:
            raise ClaimTransitionError("Empty claim transitions are not meaningful.")
        if any(type(release) is not ClaimRelease for release in self.releases):
            raise ClaimTransitionError("Releases must carry exact generation records.")
        resources = [*self.claims, *(release.resource for release in self.releases)]
        for resource in resources:
            _resource(resource)
        if len(set(resources)) != len(resources):
            raise ClaimTransitionError("Duplicate or overlapping resources are not supported.")
        if self.claims:
            _generation(self.generation)
        elif self.generation is not None:
            raise ClaimTransitionError("A release-only envelope cannot mint a generation.")
        if self.claim_scope is not None and (
            type(self.claim_scope) is not WakeClaim or not self.claims or self.releases
        ):
            raise ClaimTransitionError("Wake claim_scope requires a claim-only transition.")

    def require_new_operation(self, is_used: Callable[[str], bool]) -> None:
        """A selected operation stays consumed even after its resources release."""
        if self.claim_scope is not None and is_used(self.claim_scope.operation_id):
            raise IdentityConflict("Wake claim operation was already consumed")

    @property
    def resources(self) -> tuple[str, ...]:
        return (*self.claims, *(release.resource for release in self.releases))

    def claimed(self, resource: str) -> ClaimOwner:
        """Derive a current owner from this original acquisition, never another schema."""
        if resource not in self.claims or self.generation is None:
            raise ClaimTransitionError("Resource is not part of this claim acquisition.")
        return ClaimOwner(resource, self.owner, self.incarnation, self.generation,
                          self.seq, self.message_id, self.claim_scope)

    def apply(self, previous: ClaimProjection) -> ClaimProjection:
        """Apply atomically to the affected certified resources.

        Unrelated resources do not affect legality. The checkpoint supplies only
        the touched rows and the original claim high-water, not a replayed bus.
        """
        if self.seq <= previous.last_seq:
            raise ClaimTransitionError("Claim sequence must increase.")
        next_state = dict(previous)
        for release in self.releases:
            current = next_state.get(release.resource)
            if current is None:
                raise ClaimTransitionError("Cannot release a resource without a live claim.")
            # Renaming preserves incarnation; exact generation fences release
            # after somebody else acquires this same resource.
            if (current.incarnation, current.generation) != (self.incarnation, release.generation):
                raise ClaimTransitionError("Only the exact owner incarnation/generation may release.")
        for resource in self.claims:
            if existing := next_state.get(resource):
                raise ClaimConflict(existing)
        for release in self.releases:
            del next_state[release.resource]
        for resource in self.claims:
            next_state[resource] = self.claimed(resource)
        return ClaimProjection(self.seq, next_state)


@dataclass(frozen=True, slots=True)
class ClaimOwner:
    resource: str
    owner: str
    incarnation: str
    generation: str
    seq: int
    message_id: str
    claim_scope: WakeClaim | None = field(default=None, metadata={"wire_name": 'admission'})

    def __post_init__(self) -> None:
        _resource(self.resource)
        _text(self.owner, "Owner")
        _text(self.incarnation, "Incarnation")
        _generation(self.generation)
        _text(self.message_id, "Message ID")
        if type(self.seq) is not int or self.seq <= 0:
            raise ClaimTransitionError("Claim sequence must be a positive exact integer.")
        if self.claim_scope is not None and type(self.claim_scope) is not WakeClaim:
            raise ClaimTransitionError("Claim owner requires a typed wake claim_scope.")


@dataclass(frozen=True, slots=True)
class ClaimProjection(Mapping[str, ClaimOwner]):
    """Immutable sorted claim table plus the last processed envelope sequence."""

    last_seq: int = 0
    _claims: Mapping[str, ClaimOwner] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.last_seq) is not int or self.last_seq < 0:
            raise ClaimTransitionError("Projection sequence must be a nonnegative exact integer.")
        if not isinstance(self._claims, Mapping) or any(
            type(value) is not ClaimOwner or value.resource != key or value.seq > self.last_seq
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


def _release_resource(worktree: Path, path: str | Path) -> str:
    """Identify a prior absolute claim even if its original file was deleted."""
    physical_root = worktree.resolve(strict=True)
    raw = path if isinstance(path, str) else str(path)
    supplied = Path(raw)
    if str(supplied) != raw:
        raise RelationViolationError("Claim release requires canonical spelling.")
    if ".." in supplied.parts:
        raise RelationViolationError("Claim release cannot traverse parent directories.")
    candidate = supplied if supplied.is_absolute() else physical_root / supplied
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as error:
        raise RelationViolationError(f"Cannot resolve claim release {candidate}: {error}") from error
    if resolved != candidate:
        raise RelationViolationError("Claim release cannot use symlink aliases.")
    return str(resolved)
