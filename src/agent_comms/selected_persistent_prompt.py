"""Default-OFF transport input for an already-live persistent ACP Pi child.

This is deliberately not source, claim, tool, or terminal authority. The ACP
owner must reserve and authenticate the input and supply a one-use final pipe
boundary; the backend only sends exact bytes to the *same* live child. In
particular no native Pi child is created to replace an absent/stale owner.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SelectedPersistentPrompt:
    input_id: str
    send_boundary: Callable[[], AbstractContextManager[None]]

    def __post_init__(self) -> None:
        if type(self.input_id) is not str or re.fullmatch(r"[0-9a-f]{32}", self.input_id) is None:
            raise ValueError("selected input requires an exact reserved 32-hex ID")
        if not callable(self.send_boundary):
            raise TypeError("selected input requires an owner-supplied final pipe boundary")
