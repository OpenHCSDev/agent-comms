from __future__ import annotations

from abc import ABC, abstractmethod


class ReadsThreadIncarnation[Identity](ABC):
    """Provide a recorded or birthless identity without granting live ownership."""

    @property
    @abstractmethod
    def incarnation(self) -> Identity: ...
