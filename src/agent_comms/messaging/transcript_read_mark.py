"""Human acknowledgement of original native transcript bytes."""
from abc import ABC, abstractmethod

class MarksTranscriptRead(ABC):
    @abstractmethod
    def mark_transcript(self, viewer: str, source: str, inode: int, through: int, size: int) -> None: ...
