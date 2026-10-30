"""The captured thread identity and its declared channel membership."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from .channel_targets import Tag
from .field_codec import RegistrationPolicy
from .registration_inheritance import InheritEmpty
from .thread_identity import ThreadBirthRecord


@dataclass(frozen=True, kw_only=True)
class ThreadMembership(ThreadBirthRecord):
    """The original thread identity and its declared channel tags."""

    tags: Annotated[frozenset[str], RegistrationPolicy(InheritEmpty)] = field(kw_only=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        for tag in self.tags:
            Tag.validate(tag)
