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
from contextlib import AbstractContextManager, ExitStack
from .diagnostics import PublicationMeasurements

_MAX_SEND_SECONDS = 5.0


class PromptSendFailure(RuntimeError):  # noqa: N818 - original writer failure
    """Failure from the one owning raw writer, never permission to replay."""


class PromptSendUnknown(PromptSendFailure):  # noqa: N818 - nominal UNKNOWN outcome
    """The prompt send has no successful completion receipt; never resend."""


class PromptSendNotWritten(PromptSendFailure):  # noqa: N818 - original prebyte refusal
    """The writer never acquired its original admission or wrote this payload."""


class PromptAdmissionBusy(RuntimeError):  # noqa: N818 - nominal pre-admission outcome
    """Exclusion was unavailable before admission, mutation or any prompt byte."""


def _enter_admission(
    fd: int,
    boundary: Callable[[], AbstractContextManager[None]],
    cancelled: threading.Event,
    measurements: PublicationMeasurements,
) -> ExitStack:
    """Acquire cancellable admission without spending an irreversible-write budget.

    Every refused probe closes its partial custody. Only the successful original
    boundary can admit this payload; cancellation here proves no prompt write.
    """
    busy: PromptAdmissionBusy | None = None
    pipe = select.poll()
    pipe.register(fd, select.POLLERR | select.POLLHUP | select.POLLNVAL)
    while True:
        if cancelled.is_set():
            raise PromptSendNotWritten(
                "Native prompt admission cancelled before writing any bytes"
            ) from busy
        if pipe.poll(0):
            raise PromptSendNotWritten("Native pipe closed before prompt admission")
        scope = ExitStack()
        try:
            with measurements.operation("admission_probe"):
                scope.enter_context(boundary())
        except PromptAdmissionBusy as error:
            busy = error
            scope.close()
            cancelled.wait(0.01)
        except BaseException:
            scope.close()
            raise
        else:
            return scope


def _write_fenced(
    fd: int,
    payload: bytes,
    boundary: Callable[[], AbstractContextManager[None]],
    cancelled: threading.Event,
    timeout: float,
    measurements: PublicationMeasurements,
) -> None:
    # The dedicated thread has no asyncio loop. Its timeout progresses even if
    # the owner loop is synchronously waiting to stop this registry incarnation.
    written = 0
    try:
        with measurements.operation("admission_wait"):
            admitted = _enter_admission(fd, boundary, cancelled, measurements)
        with admitted:
            deadline = time.monotonic() + timeout
            remaining = memoryview(payload)
            while remaining:
                budget = deadline - time.monotonic()
                if cancelled.is_set() or budget <= 0:
                    raise PromptSendUnknown(
                        "Native prompt send is UNKNOWN after cancellation/deadline"
                    )
                try:
                    with measurements.operation("raw_pipe_write"):
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
                "Native prompt post-write outcome is UNKNOWN; no retry: "
                f"{type(error).__name__}: {error}"
            ) from error
        raise


async def send_fenced_prompt(
    stdin: asyncio.StreamWriter,
    payload: bytes,
    boundary: Callable[[], AbstractContextManager[None]],
    *,
    measurements: PublicationMeasurements,
    timeout: float = _MAX_SEND_SECONDS,
) -> None:
    """Own a duplicated nonblocking pipe fd until the writer has stopped.

    Cancellation is signalled to the independent writer and joined before this
    function exits. A dedicated thread avoids default-executor queue starvation.
    Each admission probe is nonblocking and uses thread-local database connections.
    Contention waits for original admission without retaining partial locks or
    depending on owner-loop callbacks. The write budget begins only after grant.
    Cancellation or a closed original pipe before grant proves no bytes. No
    admitted send is retried.
    """
    if os.name != "posix" or not payload or timeout <= 0:
        raise PromptSendNotWritten("Fenced prompt requires a live bounded POSIX pipe")
    pipe = stdin.get_extra_info("pipe")
    transport = stdin.transport
    if pipe is None or transport.get_write_buffer_size() != 0:
        raise PromptSendNotWritten("Native stdin is not an empty raw pipe; no prompt sent")
    fd = os.dup(pipe.fileno())
    if os.get_blocking(fd):
        os.close(fd)
        raise PromptSendNotWritten("Native stdin must be nonblocking; no prompt sent")
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()
    cancelled = threading.Event()
    write_timeout = min(timeout, _MAX_SEND_SECONDS)

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
            _write_fenced(fd, payload, boundary, cancelled, write_timeout, measurements)
        except BaseException as caught:
            error = caught
        finally:
            try:
                os.close(fd)
            except OSError as caught:
                # A close failure never replaces the write's own outcome.
                if error is None:
                    error = caught
                else:
                    error.add_note(f"Closing the native prompt fd also failed: {caught!r}")
        loop.call_soon_threadsafe(finish, error)

    writer = threading.Thread(target=worker, name="native-prompt-writer", daemon=False)
    try:
        writer.start()
    except BaseException:
        os.close(fd)
        raise
    try:
        await asyncio.shield(done)
    except asyncio.CancelledError as cancellation:
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
        # Cancellation still propagates, carrying the writer's own outcome
        # (for example an UNKNOWN partial write) instead of discarding it.
        if not done.cancelled() and (outcome := done.exception()) is not None:
            cancellation.__cause__ = outcome
        raise
