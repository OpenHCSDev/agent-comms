"""Declared bounded queries consume original sources without owning delivery state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Self

from .bus_publication import StableLookupText
from .coordination_errors import IdentityConflict
from .field_codec import FieldCodec


@dataclass(frozen=True, kw_only=True)
class SourcePage:
    after_seq: int = 0
    limit: int = 100

    def __post_init__(self) -> None:
        if self.after_seq < 0:
            raise ValueError("Source page sequence must be nonnegative")
        if not 1 <= self.limit <= 100:
            raise ValueError("Source page limit must be between 1 and 100")

    @classmethod
    def capture(cls, **values) -> Self:
        try:
            return FieldCodec.decode(cls, values)
        except TypeError as error:
            raise ValueError("Source page requires its exact declared fields") from error


@dataclass(frozen=True, kw_only=True)
class AddressedPage(SourcePage):
    lookup: Annotated[str, StableLookupText]


@dataclass(frozen=True, kw_only=True)
class CoveragePage(AddressedPage):
    partial: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.after_seq and not self.partial:
            raise ValueError("Noninitial coverage page must declare partial coverage")

    def require_exhausted(self, has_more: bool) -> None:
        """An unqualified coverage request cannot admit a truncated prefix."""
        if has_more and not self.partial:
            raise IdentityConflict("source coverage exceeded its bounded private initial scan")


@dataclass(frozen=True, kw_only=True)
class CandidateQuery(SourcePage):
    root_id: str
    recipient_lookup: str
    required_through_seq: int
    delivery_only: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.recipient_lookup:
            raise ValueError("Candidate query needs its original recipient")
        if self.required_through_seq < 0:
            raise ValueError("Candidate query high-water must be nonnegative")
