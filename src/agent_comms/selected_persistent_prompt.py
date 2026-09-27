"""Default-OFF transport input for an already-live persistent ACP Pi child.

This is deliberately not source, claim, tool, or terminal authority. The ACP
owner must reserve and authenticate the input and supply a one-use final pipe
boundary; the backend only sends exact bytes to the *same* live child. In
particular no native Pi child is created to replace an absent/stale owner.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field

from .native_prompt_send import PromptSendUnknown


@dataclass(frozen=True, slots=True)
class SelectedPersistentPrompt:
    input_id: str
    send_boundary: Callable[[], AbstractContextManager[None]]
    _used: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.input_id) is not str or re.fullmatch(r"[0-9a-f]{32}", self.input_id) is None:
            raise ValueError("selected input requires an exact reserved 32-hex ID")
        if not callable(self.send_boundary):
            raise TypeError("selected input requires an owner-supplied final pipe boundary")
        # PR112 maintenance exclusion and private source/owner checks must be
        # ONE owner-supplied raw-writer boundary. An outer event-loop lock is
        # insufficient and nesting two wire locks can deadlock. This marker is
        # an internal integration contract, not itself a security credential.
        if getattr(self.send_boundary, "_maintenance_wire_locked", False) is not True:
            raise ValueError("selected final pipe boundary lacks canonical wire exclusion")

    @contextmanager
    def one_use_boundary(self) -> Iterator[None]:
        """Deny legacy Boolean grants and reuse before any selected raw byte.

        This local gate is only defense in depth; the owner must also consume
        the reserved input durably under canonical locks across processes.
        """
        if not self._used.acquire(blocking=False):
            raise PromptSendUnknown("Selected input transport cannot be reused; no replay")
        # Deliberately never release: even an admission failure is UNKNOWN to
        # a later caller and cannot create a retry capability.
        try:
            with self.send_boundary() as grant:
                if grant is not None:
                    raise PromptSendUnknown("Selected input boundary returned a non-None grant")
                yield
        except PromptSendUnknown:
            raise
        except Exception as error:
            raise PromptSendUnknown("Selected input boundary failed; no replay") from error
