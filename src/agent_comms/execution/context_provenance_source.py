"""An original input supplies its own determining context provenance."""

from abc import ABC, abstractmethod


class ContextProvenanceSource[Source](ABC):
    __slots__ = ()

    @abstractmethod
    def context_provenance(self) -> Source: ...
