"""One-use fenced raw prompt writes, independent of the owner's asyncio loop.

The irreversible byte admission point is each successful os.write into the
native pipe while the admission scope is held. No StreamWriter prompt buffer
exists to flush after that scope. Partial/failed/timed-out/cancelled sends grant
no acceptance and are never retried. The reserved input remains unreplayable.
"""

from __future__ import annotations

import asyncio
import os
import select
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress

_MAX_SEND_SECONDS = 5.0


class PromptSendUnknown(RuntimeError):  # noqa: N818 - nominal UNKNOWN outcome
    """The prompt send has no successful completion receipt; never resend."""


def _write_fenced(
    fd: int,
    payload: bytes,
    boundary: Callable[[], AbstractContextManager[None]],
    cancelled: threading.Event,
    deadline: float,
) -> None:
    # The dedicated thread has no asyncio loop. Its timeout progresses even if
    # the owner loop is synchronously waiting to stop this registry incarnation.
    written = 0
    try:
        with boundary():
            remaining = memoryview(payload)
            while remaining:
                budget = deadline - time.monotonic()
                if cancelled.is_set() or budget <= 0:
                    raise PromptSendUnknown(
                        "Native prompt send is UNKNOWN after cancellation/deadline"
                    )
                try:
                    count = os.write(fd, remaining)
                except BlockingIOError:
                    # Bounded readiness wait, not input replay or provider retry.
                    select.select([], [fd], [], min(budget, 0.05))
                    continue
                except OSError as error:
                    raise PromptSendUnknown("Native prompt write is UNKNOWN; no retry") from error
                if count <= 0:
                    raise PromptSendUnknown("Native prompt short write is UNKNOWN; no retry")
                written += count
                remaining = remaining[count:]
    except Exception as error:
        if written and not isinstance(error, PromptSendUnknown):
            raise PromptSendUnknown(
                "Native prompt post-write outcome is UNKNOWN; no retry"
            ) from error
        raise


async def send_fenced_prompt(
    stdin: asyncio.StreamWriter,
    payload: bytes,
    boundary: Callable[[], AbstractContextManager[None]],
    *,
    timeout: float,
) -> None:
    """Own a duplicated nonblocking pipe fd until the writer has stopped.

    Cancellation is signalled to the independent writer and joined before this
    function exits. A dedicated thread avoids default-executor queue starvation.
    Admission must fail nonblocking on store-lock contention and use thread-local
    database connections; it must never depend on callbacks on the owner loop.
    """
    if os.name != "posix" or not payload or timeout <= 0:
        raise PromptSendUnknown("Fenced prompt requires a live bounded POSIX pipe")
    pipe = stdin.get_extra_info("pipe")
    transport = stdin.transport
    if pipe is None or transport.get_write_buffer_size() != 0:
        raise PromptSendUnknown("Native stdin is not an empty raw pipe; no prompt sent")
    fd = os.dup(pipe.fileno())
    if os.get_blocking(fd):
        os.close(fd)
        raise PromptSendUnknown("Native stdin must be nonblocking; no prompt sent")
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()
    cancelled = threading.Event()
    deadline = time.monotonic() + min(timeout, _MAX_SEND_SECONDS)

    def finish(error: BaseException | None) -> None:
        # Completion is posted only after admission and the raw fd are closed;
        # no owner-loop work is needed for this final thread exit.
        writer.join()
        if error is None:
            done.set_result(None)
        else:
            done.set_exception(error)

    def worker() -> None:
        error: BaseException | None = None
        try:
            _write_fenced(fd, payload, boundary, cancelled, deadline)
        except BaseException as caught:
            error = caught
        finally:
            try:
                os.close(fd)
            except OSError as caught:
                error = caught
        loop.call_soon_threadsafe(finish, error)

    writer = threading.Thread(target=worker, name="native-prompt-writer", daemon=False)
    try:
        writer.start()
    except BaseException:
        os.close(fd)
        raise
    try:
        await asyncio.shield(done)
    except asyncio.CancelledError:
        cancelled.set()
        # Repeated cancellation cannot abandon a still-writing fd or release
        # admission while bytes remain scheduled for transmission.
        while not done.done():
            try:
                await asyncio.shield(done)
            except asyncio.CancelledError:
                cancelled.set()
            except Exception:
                break
        if done.done():
            with suppress(BaseException):
                done.result()
        raise
