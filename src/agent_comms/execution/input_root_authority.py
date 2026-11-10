from __future__ import annotations

from abc import ABC, abstractmethod

from agent_comms.nominal.field_codec import RootId


class InputRootAuthority(ABC):
    __slots__ = ()

    @property
    @abstractmethod
    def namespace(self) -> RootId:
        """Require the original native namespace; a public registry has none."""

    @abstractmethod
    def require_root(self, root_id: RootId, error: Exception) -> None: ...
