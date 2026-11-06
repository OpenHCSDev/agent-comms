"""Bounded readiness wait on one file descriptor.

``select.select`` refuses any descriptor at or above FD_SETSIZE (1024), so a long
process that has opened many files fails there whatever the caller does. ``poll``
has no such ceiling.
"""

from __future__ import annotations

import math
import select
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _typeshed import FileDescriptorLike


def ready(target: FileDescriptorLike, timeout: float, *, write: bool = False) -> bool:
    """True once ``target`` is readable (or writable) within ``timeout`` seconds."""
    pending = select.poll()
    pending.register(target, select.POLLOUT if write else select.POLLIN)
    return bool(pending.poll(max(0, math.ceil(timeout * 1000))))
