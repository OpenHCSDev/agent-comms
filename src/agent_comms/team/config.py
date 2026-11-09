"""Install choices configure the native registry project; no teammate file is read."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from agent_comms.nominal.field_codec import (
    AbsolutePathText,
    DeclaredWireType,
    FieldCodec,
    NullMember,
    SubclassUnion,
)
from agent_comms.projects.repo import Repo, RepositorySelection


class RepoChoice(RepositorySelection, SubclassUnion, affix="Repos"):
    """Explicit repository checkouts or the project's current native repositories."""


@dataclass(frozen=True, slots=True)
class GivenRepos(DeclaredWireType, RepoChoice):
    wire_type = str  # comma-separated checkouts
    checkouts: tuple[Path, ...]

    @classmethod
    def from_wire(cls, data: object) -> Self:
        text = FieldCodec.decode(cls.wire_type, data)
        return cls(tuple(AbsolutePathText.decode(part.strip())
                         for part in text.split(",") if part.strip()))

    def to_wire(self) -> str:
        return ",".join(map(str, self.checkouts))

    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]:
        return tuple(Repo.of(checkout) for checkout in self.checkouts)

    def founded[A, F](self, absent: A, found: Callable[[tuple[Repo, ...]], F]) -> A | F:
        return found(self.applied(()))


@dataclass(frozen=True, slots=True)
class KeptRepos(NullMember, RepoChoice):
    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]:
        return recorded

    def founded[A, F](self, absent: A, found: Callable[[tuple[Repo, ...]], F]) -> A | F:
        return absent

