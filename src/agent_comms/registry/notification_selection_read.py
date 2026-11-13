"""Read contracts shared by registry and presentation owners."""
from abc import ABC, abstractmethod

class ThreadSourceReader[Presentation](ABC):
    """A presentation asks its captured source owner to acquire its evidence."""

    @abstractmethod
    def read_source(self, name: str, ordinary: Presentation) -> Presentation: ...


class NotificationSelectionRead[Reply](ABC):
    @abstractmethod
    def read_without_assignments(self) -> Reply: ...
