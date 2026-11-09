"""A captured notification selection owns the read for absent assignments."""
from abc import ABC, abstractmethod

class NotificationSelectionRead[Reply](ABC):
    @abstractmethod
    def read_without_assignments(self) -> Reply: ...
