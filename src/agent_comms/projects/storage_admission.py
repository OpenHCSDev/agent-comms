"""The operations a decoded storage effect may ask its admission context to perform."""

from abc import ABC, abstractmethod
from pathlib import Path

from agent_comms.projects.repository_sources import RepositorySource


class StorageAdmission(ABC):
    """Effects depend on admission operations, not the composition root implementing them."""

    @abstractmethod
    def require_write(self, path: Path) -> None: ...

    @abstractmethod
    def deny(self, reason: str) -> None: ...

    def deny_repository_operation(self, reason: str) -> None:
        """Non-Core admission contexts keep the refusal without tool discovery."""
        self.deny(reason)

    @abstractmethod
    def require_floor(self) -> None: ...

    @abstractmethod
    def require_checkout(self, path: Path, repository: RepositorySource) -> None: ...

    @abstractmethod
    def require_repository_donors(self, repository: RepositorySource) -> None: ...

    @abstractmethod
    def remove_checkout(self, path: Path) -> None: ...
