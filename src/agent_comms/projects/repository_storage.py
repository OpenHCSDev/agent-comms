"""The donor-path contract repository sources need from their storage owner."""

from abc import ABC, abstractmethod
from pathlib import Path

from agent_comms.projects.repository_name import RepositoryName


class RepositoryStorage[Owner](ABC):
    """A source asks for its caller's donor without depending on the full storage rule."""

    __slots__ = ()

    @abstractmethod
    def repository_donor(self, owner: Owner, repository: RepositoryName) -> Path: ...

    @abstractmethod
    def require_repository_donor(
        self, owner: Owner, repository: RepositoryName, path: Path,
    ) -> None: ...


class RepositoryReader(ABC):
    """A declared tool that can show its actual read-only repository call."""

    @classmethod
    @abstractmethod
    def example(cls, root: Path) -> str: ...
