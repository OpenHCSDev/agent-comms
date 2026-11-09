from __future__ import annotations

from abc import ABC
from collections.abc import Callable
from pathlib import Path

from agent_comms.execution.input_lookup import InputLookup


class InputTextContext[Attempt](ABC):
    __slots__ = ()
    root: Path
    input_dispositions: Callable[[Path], InputLookup[Attempt]]
