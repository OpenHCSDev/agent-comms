"""Thread presentation: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .presentation import MessageNotification


@dataclass(frozen=True, slots=True)
class ThreadPresentation:
    title: str
    marker: str
    summary: str
    busy: bool = False
    notifications: tuple[MessageNotification, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.marker} {self.title}"
