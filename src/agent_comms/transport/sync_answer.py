"""A synchronous entry point that answers with asynchronous work."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any


class AwaitsWork:
    """A synchronous entry point whose answer comes from a coroutine.

    `asyncio.run` refuses when the calling thread already has a running loop, so a caller
    that used it answered or raised depending on where it happened to run rather than on
    what it was asked. That is how a declared read answered on the observer's delivery loop
    crashed its child while the same request answered fine from a UI's own thread.

    A thread of its own never has a loop, so the answer stops depending on the caller's
    surroundings. Callers already made their own loop; this only removes the precondition
    that nobody above them had one.
    """

    def run_awaited[T](self, work: Coroutine[Any, Any, T]) -> T:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, work).result()
