"""Read the selected idle child's actual compaction policy without starting input."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

from .backend import PersistentPiSession
from .native_pi import NativePiUnavailable
from .native_session_reopen import NativeSessionIdentity
from .owner_compaction_settings import PiCompactionDecision
from .pi_commands import AgentCommsCompactionSettings, PiCommand
from .pi_events import Response
from .pi_rpc import PiRpcChannel
from .pi_summary_payloads import SelectedModel


class SelectedPiProbeUnknownError(RuntimeError):
    """A sent or untrusted probe is not retry/commit/input authority."""


_Observation = TypeVar("_Observation")


async def _exchange_observation(
    persistent: PersistentPiSession,
    request: PiCommand,
    source: NativeSessionIdentity,
    decode: Callable[[bytes, PiCommand], _Observation],
    *,
    expected_package: Path,
    timeout: float,
    max_response: int | None = None,
) -> _Observation:
    """One read-only request; every uncertain transport retires the borrowed child."""
    if not 0 < timeout <= 5:
        raise ValueError("Bounded selected Pi deadline required")
    async with persistent.lock:
        try:
            retained = persistent.custody.idle().selected(
                source, expected_package
            )
        except NativePiUnavailable as error:
            raise SelectedPiProbeUnknownError(str(error)) from error
        proc, reader = retained.child.proc, retained.child.reader
        transmitted = False
        try:
            proc.stdin.write(PiRpcChannel.command_bytes(request))
            transmitted = True  # Even a failed drain can have put bytes on the pipe.
            async with asyncio.timeout(timeout):
                await proc.stdin.drain()
                raw = (
                    await reader.readline()
                    if max_response is None
                    else await reader.readline(max_bytes=max_response)
                )
            outcome = decode(raw, request)
            if not retained.current:
                raise SelectedPiProbeUnknownError("Selected Pi source changed during dry run")
            return outcome
        except BaseException as error:
            if transmitted:
                # Poison before a cancellable await. No next borrower may use
                # old in-memory history or treat this as a paid-summary receipt.
                persistent.require_reopen(source)
                # close() retains its independently shielded reap task if
                # cancellation interrupts this caller's join.
                with suppress(asyncio.CancelledError):
                    await persistent.close()
            if isinstance(error, (asyncio.CancelledError, SelectedPiProbeUnknownError)):
                raise
            raise SelectedPiProbeUnknownError("Selected Pi dry-run transport uncertain") from error


def _read_settings_response(
    raw: bytes, request: AgentCommsCompactionSettings
) -> PiCompactionDecision:
    if not raw.endswith(b"\n") or len(raw) > 16384:
        raise SelectedPiProbeUnknownError("Incomplete selected settings response")
    response = PiRpcChannel.decode_record(raw, strict=True, max_bytes=16384)
    return response.require_request(request).require_request(request)


async def read_selected_compaction_decision(
    persistent: PersistentPiSession,
    *,
    session_file: str,
    expected_package: Path,
    selected: SelectedModel,
    timeout: float = 3.0,
) -> PiCompactionDecision:
    """Observe actual selected settings/model without auth, provider or input writes."""
    source = persistent.custody.idle().identity
    source.require_session(session_file)
    request = AgentCommsCompactionSettings(
        id=secrets.token_hex(16),
        session_id=source.session_id,
        session_file=source.session_file,
        selected=selected,
    )
    return await _exchange_observation(
        persistent,
        request,
        source,
        _read_settings_response,
        expected_package=expected_package,
        timeout=timeout,
        max_response=16384,
    )
