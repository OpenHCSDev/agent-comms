"""The runtime coordinates and acknowledgement a transcript cursor consumes."""
from abc import ABC, abstractmethod
from collections.abc import Iterable

class TranscriptRuntime(ABC):
    @property
    @abstractmethod
    def read_key(self) -> str | None: ...

    @abstractmethod
    def read_keys(self) -> Iterable[str]: ...

class MarksSourceRead[Cut](ABC):
    @abstractmethod
    def mark_read(self, viewer: str, source: str, through: int, cut: Cut) -> None: ...
