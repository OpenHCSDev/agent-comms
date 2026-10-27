"""One declaration per bus read: the operation owns its arguments and its read once.

A ``BusRead`` member is the operation. Each boundary that serves it keeps only what is
its own: the UI its root, request id and refusals (``ReadRequest``), MCP its caller,
exposure and reply (``ToolRequest``). Neither boundary restates the read, and the
reader's authority reaches the operation as a ``BusReader`` the boundary supplies,
never as an argument the caller may choose.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .presentation import AllOutcomes

if TYPE_CHECKING:
    from .comms import Comms
    from .presentation import OutcomeViewer


class BusReader(ABC):
    """Whose authority a read runs under; each boundary supplies its own reader."""

    @abstractmethod
    def outcome_viewer(self, comms: Comms) -> OutcomeViewer:
        """Whose recipient outcomes this reader may read."""


class OperatorOutcomes:
    """A reader speaking for the owner reads every recipient outcome, as the owner's UI does."""

    __slots__ = ()

    def outcome_viewer(self, comms: Comms) -> OutcomeViewer:
        return AllOutcomes()


class OperatorReader(OperatorOutcomes, BusReader):
    """The owner's UI: it observes the whole bus as the operator."""

    __slots__ = ()


class BusRead[Result](DeclaredFamily, ABC, affix=""):
    """A read of Core's stores, declared once with its arguments; boundaries derive it."""

    @abstractmethod
    def read_as(self, comms: Comms, reader: BusReader) -> Result:
        """The read under the reader's authority."""


class UiRead[Result](BusRead[Result], ABC):
    """The UI serves this read as the operator; its envelope (root, request id, refusal)
    stays on ``ReadRequest``."""

    def read(self, comms: Comms) -> Result:
        return self.read_as(comms, OperatorReader())
