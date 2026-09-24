"""Pure, default-off claim transitions for a *future* single-authority bus envelope.

No bus send, file append, O_EXCL claim sidecar, write fence, or retry lives here.
A caller must serialize send and every claim-bearing read under the wire→bus
locks, admit only newline-complete rows after fsync of the opened bus file (and
its parent on creation), and quarantine/re-fsync a visible row after failed
fsync before any reader observes it. Parsed rows alone are NOT durable proof.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType


class ClaimTransitionError(ValueError):
    """An invalid request or unverified/corrupt projection input."""


class ClaimConflict(ClaimTransitionError):  # noqa: N818 - domain-specific losing result
    """A synchronous losing transition; never queue or retry it automatically."""

    def __init__(self, existing: ClaimOwner):
        self.existing = existing
        super().__init__(f"Resource already claimed by {existing.owner!r}")


def normalize_existing_file(root: Path, resource: str | Path) -> str:
    """Canonical physical worktree/file identity; no symlink or hardlink aliases.

    Only existing regular files are supported. The physical worktree root is
    part of the identifier; distinct physical worktrees do not conflict.
    """
    if not (type(resource) is str or isinstance(resource, Path)) or not str(resource):
        raise ClaimTransitionError("Resource must be a nonempty path.")
    base = Path(root).absolute()
    try:
        physical_root = base.resolve(strict=True)
        if physical_root != base or not base.is_dir():
            raise ClaimTransitionError("Worktree must be a physical existing directory.")
        requested = Path(resource)
        if ".." in requested.parts:
            raise ClaimTransitionError("Parent traversal is not a resource identifier.")
        candidate = requested if requested.is_absolute() else base / requested
        resolved = candidate.resolve(strict=True)
        if resolved != candidate:
            raise ClaimTransitionError("Symlink aliases are not supported.")
        relative = resolved.relative_to(physical_root)
        if not relative.parts:
            raise ClaimTransitionError("Worktree directories cannot be claimed.")
        info = resolved.stat()
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, ClaimTransitionError):
            raise
        raise ClaimTransitionError(
            "Resource must be an existing file inside the worktree."
        ) from error
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ClaimTransitionError("Only existing singly-linked regular files are supported.")
    return str(resolved)


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
class ClaimRelease:
    resource: str
    generation: str

    def __post_init__(self) -> None:
        _resource(self.resource)
        _generation(self.generation)


@dataclass(frozen=True, slots=True)
class ClaimTransition:
    """Claims and releases in ONE future message row, never independent writes."""

    owner: str
    incarnation: str
    seq: int
    message_id: str
    claims: tuple[str, ...] = ()
    releases: tuple[ClaimRelease, ...] = ()
    generation: str | None = None

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


@dataclass(frozen=True, slots=True)
class ClaimOwner:
    resource: str
    owner: str
    incarnation: str
    generation: str
    seq: int
    message_id: str


@dataclass(frozen=True, slots=True)
class ClaimProjection(Mapping[str, ClaimOwner]):
    """Immutable sorted claim table plus the last processed envelope sequence."""

    last_seq: int = 0
    _claims: Mapping[str, ClaimOwner] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.last_seq) is not int or self.last_seq < 0:
            raise ClaimTransitionError("Projection sequence must be a nonnegative exact integer.")
        if not isinstance(self._claims, Mapping) or any(
            type(key) is not str
            or type(value) is not ClaimOwner
            or value.resource != key
            or type(value.seq) is not int
            or value.seq <= 0
            or value.seq > self.last_seq
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


def apply_transition(previous: ClaimProjection, transition: ClaimTransition) -> ClaimProjection:
    """All-or-nothing immutable projection from caller-verified durable envelopes.

    The caller MUST hold the wire→bus locks and verify bus fsync/newline and
    outer sender/sequence/message identity and unique registered creation
    incarnations. Neither this method nor a parser can determine whether an
    observed row was actually durable or whether two names are distinct owners.
    """
    if type(previous) is not ClaimProjection or type(transition) is not ClaimTransition:
        raise ClaimTransitionError("Only typed claim projections/transitions are accepted.")
    if transition.seq <= previous.last_seq:
        raise ClaimTransitionError("Claim sequence must increase.")
    next_state = dict(previous)
    for release in transition.releases:
        current = next_state.get(release.resource)
        if current is None:
            raise ClaimTransitionError("Cannot release a resource without a live claim.")
        # The envelope owner is the sender's name at publication time. Renaming
        # preserves the owner's incarnation, but must not strand an older claim
        # whose envelope recorded a previous name. The generation still fences
        # release after another owner has acquired the same resource.
        if (current.incarnation, current.generation) != (
            transition.incarnation,
            release.generation,
        ):
            raise ClaimTransitionError("Only the exact owner incarnation/generation may release.")
    for resource in transition.claims:
        if existing := next_state.get(resource):
            raise ClaimConflict(existing)
    for release in transition.releases:
        del next_state[release.resource]
    for resource in transition.claims:
        assert transition.generation is not None
        next_state[resource] = ClaimOwner(
            resource,
            transition.owner,
            transition.incarnation,
            transition.generation,
            transition.seq,
            transition.message_id,
        )
    return ClaimProjection(transition.seq, next_state)


def project_verified_transitions(rows: Iterable[ClaimTransition]) -> ClaimProjection:
    """Replay ONLY rows whose complete-line durability was verified by the caller.

    Do not pass raw JSONL or accept a claimed `fsynced` boolean as proof.
    The future guarded bus reader must supply *all* claim rows in increasing
    sequence, including release-only rows after the last live claim.
    """
    projection = ClaimProjection()
    for transition in rows:
        projection = apply_transition(projection, transition)
    return projection


def parse_complete_transition_line(raw: bytes) -> ClaimTransition:
    """Syntax-only fixture parser; newline/JSON checks DO NOT establish fsync."""
    if type(raw) is not bytes or not raw.endswith(b"\n") or len(raw) > 16_384:
        raise ClaimTransitionError("Transition line must be bounded and newline-complete.")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        fields: dict[str, object] = {}
        for key, value in pairs:
            if key in fields:
                raise ClaimTransitionError("Duplicate JSON transition field.")
            fields[key] = value
        return fields

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except ClaimTransitionError:
        raise
    except (UnicodeError, ValueError) as error:
        raise ClaimTransitionError("Invalid transition JSON.") from error
    required = {"owner", "incarnation", "seq", "message_id", "claims", "releases", "generation"}
    if type(data) is not dict or set(data) != required:
        raise ClaimTransitionError("Transition has missing or unknown fields.")
    if type(data["claims"]) is not list or type(data["releases"]) is not list:
        raise ClaimTransitionError("Transition resources must be arrays.")
    releases: list[ClaimRelease] = []
    for release in data["releases"]:
        if type(release) is not dict or set(release) != {"resource", "generation"}:
            raise ClaimTransitionError("Invalid release record.")
        releases.append(ClaimRelease(release["resource"], release["generation"]))
    return ClaimTransition(
        data["owner"],
        data["incarnation"],
        data["seq"],
        data["message_id"],
        tuple(data["claims"]),
        tuple(releases),
        data["generation"],
    )
