"""The stable participant lookup shared by registry and coordinator identities."""

from abc import ABC, abstractmethod

from .field_codec import ThreadLookup


class ParticipantIdentity(ABC):
    """Naming a participant does not grant a live generation or replay permission."""

    __slots__ = ()

    @property
    @abstractmethod
    def lookup(self) -> ThreadLookup: ...
