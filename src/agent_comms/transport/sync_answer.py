"""A synchronous caller's answer to asynchronous work."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def run_awaited[T](work: Coroutine[Any, Any, T]) -> T:
    """Run `work` to completion and hand back its answer.

    `asyncio.run` refuses when the calling thread already has a running loop, so a caller
    that used it answered correctly or raised depending on where it happened to run, not on
    what it was asked. A thread of its own never has a loop, so the answer no longer depends
    on the caller's surroundings. Callers already made their own loop; this only removes the
    precondition that nobody above them had one.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, work).result()
