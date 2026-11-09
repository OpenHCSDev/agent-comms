"""Bounded readiness wait on one file descriptor.

``select.select`` refuses any descriptor at or above FD_SETSIZE (1024), so a long
process that has opened many files fails there whatever the caller does. ``poll``
has no such ceiling.
"""

import math
import select


def ready(descriptor: int, timeout: float, *, write: bool = False) -> bool:
    """True once ``descriptor`` is readable (or writable) within ``timeout`` seconds."""
    pending = select.poll()
    pending.register(descriptor, select.POLLOUT if write else select.POLLIN)
    return bool(pending.poll(max(0, math.ceil(timeout * 1000))))
