"""Pure claim transitions for the single-authority bus envelope.

No bus send, file append, O_EXCL claim sidecar, write fence, or retry lives here.
A caller must serialize send and every claim-bearing read under the wire→bus
locks, admit only newline-complete rows after fsync of the opened bus file (and
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
from typing import TYPE_CHECKING, ClassVar, Literal, Self
from uuid import uuid4

from .coordination_errors import IdentityConflict, IntegrityViolationError
from .errors import RelationViolationError
from .message_reference import MessageReference
from .field_codec import FieldCodec, WireValue, required_projection
from .thread_identity import GenerationCounter, ThreadIncarnation, TurnId

if TYPE_CHECKING:
    from .coordination_tables.attempts import AttemptRecord
    from .messages import Message
    from .private_bus_checkpoint import CertifiedSourceRead


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
    result = _text(value, "Generation")
    if len(result) != 32 or any(char not in "0123456789abcdef" for char in result):
        raise ClaimTransitionError("Generation must be a lowercase 128-bit hex value.")
    return result


def _resource(value: object) -> str:
    path = _text(value, "Resource")
    requested = Path(path)
    if (
        str(requested) != path
        or not requested.is_absolute()
        or requested.resolve(strict=False) != requested
    ):
        raise ClaimTransitionError("Resource must already be a canonical absolute path.")
    return path


@dataclass(frozen=True, slots=True)
class AssignmentBinding:
    """Selected claim revision and source, shared by its admission and SQL row."""

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
class WakeAdmission:
    """A selected N/K execution bound into the durable resource-claim row."""

    wire_root_id: str
    source_seq: int
    source_message_id: str
    wake_assignment_id: str = field(metadata={"wire_name": "wake_claim_id"})
    wake_revision: int
    recipient_lookup: str
    execution_id: str
    operation_id: str
    owner_admission_generation: int
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
        from .bus_publication import StableLookupText
        from .wire_metadata import WireRootIdText

        WireRootIdText.decode(self.wire_root_id)
        StableLookupText.decode(self.recipient_lookup)
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
            GenerationCounter.require_positive(self.owner_admission_generation)
            GenerationCounter.require_positive(self.participant_generation)
            if FieldCodec.decode(int, self.attempt_ordinal) <= 0:
                raise ValueError("attempt ordinal must be positive")
        except ValueError as error:
            raise ClaimTransitionError("Wake admission version or revision is invalid.") from error
        TurnId.for_registration(self.turn_id)
        if (
            type(self.wake_assignment_id) is not str
            or not self.wake_assignment_id.startswith("cohort-v1:")
            or len(self.wake_assignment_id) != len("cohort-v1:") + 64
            or any(
                character not in "0123456789abcdef" for character in self.wake_assignment_id[10:]
            )
        ):
            raise ClaimTransitionError("Wake claim ID is invalid.")

    def matches_attempt(self, attempt: AttemptRecord) -> bool:
        return (self.execution_id, self.attempt_ordinal, self.recipient_lookup,
                self.participant_generation) == (
                    attempt.execution_id, attempt.attempt_ordinal,
                    attempt.owner_lookup, attempt.owner_generation)

    def require_target(self, targets: Mapping[str, str]) -> str:
        try:
            target = targets[self.wake_assignment_id]
        except KeyError as error:
            raise IntegrityViolationError('terminal claim has no original wake assignment route') from error
        if not target:
            raise IntegrityViolationError('terminal claim has no original wake assignment route')
        return target


@dataclass(frozen=True, slots=True)
class ClaimRelease:
    resource: str
    generation: str

    def __post_init__(self) -> None:
        _resource(self.resource)
        _generation(self.generation)

    @classmethod
    def capture(cls, worktree: Path, path: str | Path, source: CertifiedSourceRead) -> ClaimRelease:
        return source.claim(_release_resource(worktree, path)).release()


class ClaimBinding(WireValue, ABC):
    """A manual acquisition or an acquisition bound to one native operation."""

    @classmethod
    def from_wire(cls, data: object) -> ClaimBinding:
        bound = AdmittedClaimBinding(FieldCodec.decode(WakeAdmission, data))
        if not isinstance(bound, cls):
            raise ClaimTransitionError('Claim binding does not match its declared member')
        return bound

    @classmethod
    def schema(cls) -> dict:
        return FieldCodec.record_schema(WakeAdmission)

    @abstractmethod
    def require_transition(self, releases: tuple[ClaimRelease, ...]) -> None: ...

    @abstractmethod
    def require_new_operation(self, is_used: Callable[[str], bool]) -> None: ...

    @abstractmethod
    def owner_for(self, acquisition: AcquiringTransition, resource: str) -> ClaimOwner: ...

    @abstractmethod
    def releasing(self, message: Message, incarnation: str,
                  releases: tuple[ClaimRelease, ...]) -> ReleasingTransition: ...


@dataclass(frozen=True)
class UnboundClaimBinding(ClaimBinding):
    def to_wire(self):
        return None  # Omitted by the containing record's declared default.

    def require_transition(self, releases: tuple[ClaimRelease, ...]) -> None:
        pass

    def require_new_operation(self, is_used: Callable[[str], bool]) -> None:
        pass

    def owner_for(self, acquisition: AcquiringTransition, resource: str) -> ClaimOwner:
        return ClaimOwner.acquired(acquisition, resource)

    def releasing(self, message: Message, incarnation: str,
                  releases: tuple[ClaimRelease, ...]) -> ReleasingTransition:
        return ReleasingTransition(owner=message.sender, incarnation=incarnation,
                                   seq=message.seq, message_id=message.message_id, releases=releases)


@dataclass(frozen=True)
class AdmittedClaimBinding(ClaimBinding):
    admission: WakeAdmission

    def to_wire(self):
        return FieldCodec.encode(self.admission)

    def require_transition(self, releases: tuple[ClaimRelease, ...]) -> None:
        if releases:
            raise ClaimTransitionError('Wake admission requires a claim-only transition.')

    def require_new_operation(self, is_used: Callable[[str], bool]) -> None:
        if is_used(self.admission.operation_id):
            raise IdentityConflict('Wake claim operation was already consumed')

    def owner_for(self, acquisition: AcquiringTransition, resource: str) -> ClaimOwner:
        return AdmittedClaimOwner.acquired(acquisition, resource, admission=self.admission)

    def releasing(self, message: Message, incarnation: str,
                  releases: tuple[ClaimRelease, ...]) -> ReleasingTransition:
        raise ClaimTransitionError('Wake admission requires a claim-only transition.')


@dataclass(frozen=True, kw_only=True)
class ClaimTransition(WireValue, ABC):
    """One atomic claim change; the concrete operation owns its possible data."""

    max_resources: ClassVar[int] = 32
    owner: str
    incarnation: str
    seq: int
    message_id: str
    releases: tuple[ClaimRelease, ...] = field(default=(), metadata={'wire_required': True})

    def to_wire(self):
        return FieldCodec.record_encode(self)

    @classmethod
    def from_wire(cls, data: object) -> ClaimTransition:
        if not isinstance(data, dict):
            raise ClaimTransitionError('Claim transition requires an object')
        member = ReleasingTransition if data.get('generation') is None else AcquiringTransition
        transition = FieldCodec.record_decode(member, data)
        if not isinstance(transition, cls):
            raise ClaimTransitionError('Claim transition does not match its declared member')
        return transition

    @classmethod
    def schema(cls) -> dict:
        members = cls.__subclasses__()
        return ({'oneOf': [FieldCodec.record_schema(member) for member in members]}
                if members else FieldCodec.record_schema(cls))

    @classmethod
    def prepare(cls, message: Message, incarnation: str, claims: tuple[str, ...],
                releases: tuple[ClaimRelease, ...], binding: ClaimBinding) -> ClaimTransition:
        if claims:
            return AcquiringTransition(owner=message.sender, incarnation=incarnation,
                seq=message.seq, message_id=message.message_id, claims=claims,
                releases=releases, generation=uuid4().hex, binding=binding)
        return binding.releasing(message, incarnation, releases)

    def __post_init__(self) -> None:
        _text(self.owner, 'Owner')
        _text(self.incarnation, 'Incarnation')
        _text(self.message_id, 'Message ID')
        if self.seq <= 0:
            raise ClaimTransitionError('Sequence must be a positive exact integer.')
        resources = self.resources
        if not resources or len(resources) > self.max_resources:
            raise ClaimTransitionError('Claim transition requires a bounded nonempty resource set.')
        for resource in resources:
            _resource(resource)
        if len(set(resources)) != len(resources):
            raise ClaimTransitionError('Duplicate or overlapping resources are not supported.')

    def require_message(self, message: Message) -> None:
        if (self.owner, self.seq, self.message_id) != (message.sender, message.seq, message.message_id):
            raise ClaimTransitionError('Claim transition does not bind its message.')

    @property
    def resources(self) -> tuple[str, ...]:
        return (*self.acquired_resources(), *(release.resource for release in self.releases))

    @abstractmethod
    def acquired_resources(self) -> tuple[str, ...]: ...

    @abstractmethod
    def acquired_owners(self) -> tuple[ClaimOwner, ...]: ...

    @abstractmethod
    def require_new_operation(self, is_used: Callable[[str], bool]) -> None: ...

    def apply(self, previous: ClaimProjection) -> ClaimProjection:
        if self.seq <= previous.last_seq:
            raise ClaimTransitionError('Claim sequence must increase.')
        acquired = self.acquired_owners()
        for release in self.releases:
            previous.claim(release.resource).validate_release(self.incarnation, release)
        for owner in acquired:
            previous.claim(owner.resource).require_free()
        next_state = dict(previous)
        for release in self.releases:
            del next_state[release.resource]
        for owner in acquired:
            next_state[owner.resource] = owner
        return ClaimProjection(self.seq, next_state)


@dataclass(frozen=True, kw_only=True)
class AcquiringTransition(ClaimTransition):
    claims: tuple[str, ...]
    generation: str
    binding: ClaimBinding = field(default_factory=UnboundClaimBinding,
        metadata={'wire_name': 'admission', 'wire_omit_default': True, 'wire_nonnull': True})

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.claims:
            raise ClaimTransitionError('Acquiring transitions require resources.')
        _generation(self.generation)
        self.binding.require_transition(self.releases)

    def acquired_resources(self) -> tuple[str, ...]:
        return self.claims

    def acquired_owners(self) -> tuple[ClaimOwner, ...]:
        return tuple(self.claimed(resource) for resource in self.claims)

    def claimed(self, resource: str) -> ClaimOwner:
        if resource not in self.claims:
            raise ClaimTransitionError('Resource is not part of this claim acquisition.')
        return self.binding.owner_for(self, resource)

    def require_new_operation(self, is_used: Callable[[str], bool]) -> None:
        self.binding.require_new_operation(is_used)


@dataclass(frozen=True, kw_only=True)
class ReleasingTransition(ClaimTransition):
    @required_projection(view='wire', name='claims')
    def _wire_claims(self) -> tuple[()]:
        return ()

    @required_projection(view='wire', name='generation')
    def _wire_generation(self) -> None:
        return None

    def acquired_resources(self) -> tuple[str, ...]:
        return ()

    def acquired_owners(self) -> tuple[ClaimOwner, ...]:
        return ()

    def require_new_operation(self, is_used: Callable[[str], bool]) -> None:
        pass


class Claim(ABC):
    """A resource is free or held; callers never interpret a missing owner."""

    @abstractmethod
    def require_free(self) -> None: ...

    @abstractmethod
    def release(self) -> ClaimRelease: ...

    @abstractmethod
    def validate_release(self, incarnation: str, release: ClaimRelease) -> None: ...

    @abstractmethod
    def admit(self, owner: ThreadIncarnation, admission: WakeAdmission,
              acquire: Callable[[], ClaimOwner]) -> ClaimOwner: ...

    @abstractmethod
    def matches(self, owner: ClaimOwner) -> bool: ...


@dataclass(frozen=True, slots=True)
class FreeClaim(Claim):
    def require_free(self) -> None:
        pass

    def release(self) -> ClaimRelease:
        raise RelationViolationError('Cannot release an unclaimed resource.')

    def validate_release(self, incarnation: str, release: ClaimRelease) -> None:
        raise ClaimTransitionError('Cannot release a resource without a live claim.')

    def admit(self, owner: ThreadIncarnation, admission: WakeAdmission,
              acquire: Callable[[], ClaimOwner]) -> ClaimOwner:
        return acquire()

    def matches(self, owner: ClaimOwner) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ClaimOwner(Claim, WireValue):
    """The existing owner record is the held member, not a second wrapper."""
    resource: str
    owner: str
    incarnation: str
    generation: str
    seq: int
    message_id: str

    def to_wire(self):
        return FieldCodec.record_encode(self)

    @classmethod
    def from_wire(cls, data: object) -> ClaimOwner:
        if not isinstance(data, dict):
            raise ClaimTransitionError('Claim owner requires an object')
        member = AdmittedClaimOwner if 'admission' in data else ClaimOwner
        owner = FieldCodec.record_decode(member, data)
        if not isinstance(owner, cls):
            raise ClaimTransitionError('Claim owner does not match its declared member')
        return owner

    @classmethod
    def schema(cls) -> dict:
        return {'oneOf': [FieldCodec.record_schema(member)
                          for member in (cls, *cls.__subclasses__())]}

    @classmethod
    def acquired(cls, transition: AcquiringTransition, resource: str, **context: object) -> Self:
        return cls(resource, transition.owner, transition.incarnation, transition.generation,
                   transition.seq, transition.message_id, **context)

    def __post_init__(self) -> None:
        _resource(self.resource)
        _text(self.owner, "Owner")
        _text(self.incarnation, "Incarnation")
        _generation(self.generation)
        _text(self.message_id, "Message ID")
        if type(self.seq) is not int or self.seq <= 0:
            raise ClaimTransitionError("Claim sequence must be a positive exact integer.")

    def require_free(self) -> None:
        raise ClaimConflict(self)

    def release(self) -> ClaimRelease:
        return ClaimRelease(self.resource, self.generation)

    def validate_release(self, incarnation: str, release: ClaimRelease) -> None:
        if (self.incarnation, self.generation) != (incarnation, release.generation):
            raise ClaimTransitionError('Only the exact owner incarnation/generation may release.')

    def admit(self, owner: ThreadIncarnation, admission: WakeAdmission,
              acquire: Callable[[], ClaimOwner]) -> ClaimOwner:
        raise ClaimConflict(self)

    def belongs_to(self, admission: WakeAdmission) -> bool:
        return False

    def for_attempt(self, attempt: AttemptRecord) -> tuple[AdmittedClaimOwner, ...]:
        return ()

    def matches(self, owner: ClaimOwner) -> bool:
        return self == owner


@dataclass(frozen=True)
class AdmittedClaimOwner(ClaimOwner):
    admission: WakeAdmission

    def admit(self, owner: ThreadIncarnation, admission: WakeAdmission,
              acquire: Callable[[], ClaimOwner]) -> ClaimOwner:
        if (self.admission, self.incarnation, self.owner) == (admission, str(owner.created_at), owner.name):
            return self
        raise ClaimConflict(self)

    def belongs_to(self, admission: WakeAdmission) -> bool:
        return self.admission == admission

    def for_attempt(self, attempt: AttemptRecord) -> tuple[AdmittedClaimOwner, ...]:
        return (self,) if self.admission.matches_attempt(attempt) else ()


@dataclass(frozen=True, slots=True)
class ClaimProjection(Mapping[str, ClaimOwner]):
    """Immutable sorted claim table plus the last processed envelope sequence."""

    last_seq: int = 0
    _claims: Mapping[str, ClaimOwner] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.last_seq) is not int or self.last_seq < 0:
            raise ClaimTransitionError("Projection sequence must be a nonnegative exact integer.")
        if any(value.resource != key or value.seq > self.last_seq
               for key, value in self._claims.items()):
            raise ClaimTransitionError("Previous projection is malformed.")
        object.__setattr__(self, "_claims", MappingProxyType(dict(sorted(self._claims.items()))))

    def claim(self, resource: str) -> Claim:
        return self._claims.get(resource, FreeClaim())

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
