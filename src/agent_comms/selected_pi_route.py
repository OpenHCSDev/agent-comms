"""Provider-free probe of the already selected idle Pi RPC child.

This v1 dry-run response is NOT credential/header route attestation, a summary,
or commit/input authority. The patched native command is not enabled by ACP
until its exact bytes receive separate review. No subprocess is started here.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from .backend import PersistentPiSession, _session_revision
from .field_codec import FieldCodec
from .owner_compaction_prepare import NativeWitness
from .owner_compaction_settings import PiCompactionDecision, PiCompactionSettings
from .pi_commands import AgentCommsCompactionSettings, AgentCommsPrepareCompaction, PiCommand
from .pi_events import Response
from .pi_rpc import PiRpcChannel
from .pi_summary_payloads import ProbeDeclinedData, ProbeReadyData, SelectedModel


class SelectedPiProbeUnknownError(RuntimeError):
    """A sent or untrusted probe is not retry/commit/input authority."""


@dataclass(frozen=True)
class SelectedPiDryRun:
    """Readiness observation only; intentionally cannot be used as a bool grant."""

    status: Literal["ready", "declined"]
    reason: str | None
    route_status: str | None

    def __bool__(self) -> bool:
        raise TypeError("Dry-run readiness never authorizes a paid summary or native commit")


def _request(
    witness: NativeWitness, selected: dict[str, Any], settings: dict[str, Any]
) -> AgentCommsPrepareCompaction:
    return AgentCommsPrepareCompaction(
        id=secrets.token_hex(16),
        witness=witness,
        selected=SelectedModel.from_wire(selected),
        settings=FieldCodec.decode(PiCompactionSettings, settings),
    )


def _read_response(raw: bytes, request: AgentCommsPrepareCompaction) -> SelectedPiDryRun:
    if not raw or len(raw) > 8192 or not raw.endswith(b"\n"):
        raise SelectedPiProbeUnknownError("Incomplete bounded selected Pi response")
    try:
        response = PiRpcChannel.decode_record(raw, strict=True, max_bytes=8192)
        if (
            not isinstance(response, Response)
            or response.id != request.id
            or response.command is not type(request)
            or response.success is not True
        ):
            raise ValueError("Unmatched selected Pi response")
        data = response.data
        if isinstance(data, ProbeReadyData) and (
            data.witness == request.witness
            and data.selected == request.selected
            and data.settings == request.settings
        ):
            return SelectedPiDryRun("ready", None, data.route_status)
        if isinstance(data, ProbeDeclinedData):
            return SelectedPiDryRun("declined", data.reason, None)
        raise ValueError("Unrecognized selected Pi dry-run outcome")
    except (UnicodeError, ValueError, TypeError) as error:
        raise SelectedPiProbeUnknownError("Invalid selected Pi response") from error


async def probe_idle_selected_pi(
    persistent: PersistentPiSession,
    witness: NativeWitness,
    selected: dict[str, Any],
    settings: dict[str, Any],
    *,
    expected_package: Path,
    timeout: float = 3.0,
) -> SelectedPiDryRun:
    """One RPC request to an idle, existing child; NEVER starts provider work.

    A transmitted request with a missing/invalid result retires the exact old
    child and requires strict saved-session validation before a later borrow.
    The caller must not convert readiness into permission to summarize/send.
    """
    request = _request(witness, selected, settings)
    return await _exchange_observation(
        persistent,
        request,
        witness.session_file,
        witness.session_id,
        _read_response,
        expected_package=expected_package,
        timeout=timeout,
    )


_Observation = TypeVar("_Observation")


async def _exchange_observation(
    persistent: PersistentPiSession,
    request: PiCommand,
    session_file: str,
    session_id: str,
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
        proc, reader = persistent.proc, persistent.reader
        if (
            persistent.reopen_required is not None
            or proc is None
            or proc.returncode is not None
            or proc.stdin is None
            or reader is None
            or persistent.session_file != session_file
            or persistent.session_id != session_id
            or persistent.revision is None
            or persistent.revision != _session_revision(session_file)
            or persistent.launch_key is None
            or persistent.launch_key[0].package != expected_package
        ):
            raise SelectedPiProbeUnknownError("Selected idle Pi child is unavailable or stale")
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
            if proc.returncode is not None or persistent.revision != _session_revision(
                session_file
            ):
                raise SelectedPiProbeUnknownError("Selected Pi source changed during dry run")
            return outcome
        except BaseException as error:
            if transmitted:
                # Poison before a cancellable await. No next borrower may use
                # old in-memory history or treat this as a paid-summary receipt.
                persistent.reopen_required = session_file
                persistent.reopen_session_id = session_id
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
    if (
        not isinstance(response, Response)
        or response.id != request.id
        or response.command is not type(request)
        or response.success is not True
    ):
        raise SelectedPiProbeUnknownError("Unmatched selected settings response")
    data = response.data
    if (
        data is None
        or data.session_id != request.session_id
        or data.session_file != request.session_file
        or data.selected != request.selected
        or data.context_tokens != request.context_tokens
    ):
        raise SelectedPiProbeUnknownError("Selected settings source changed")
    return data.decision


async def read_selected_compaction_decision(
    persistent: PersistentPiSession,
    *,
    session_file: str,
    expected_package: Path,
    provider: str,
    model_id: str,
    context_tokens: int,
    context_window: int,
    timeout: float = 3.0,
) -> PiCompactionDecision:
    """Observe actual selected settings/model without auth, provider or input writes."""
    session_id = persistent.session_id
    if (
        not session_id
        or not session_file
        or not provider
        or not model_id
        or type(context_tokens) is not int
        or not 0 <= context_tokens <= 2**53 - 1
        or type(context_window) is not int
        or not 0 < context_window <= 2**53 - 1
    ):
        raise ValueError("Exact selected settings source required")
    request = AgentCommsCompactionSettings(
        id=secrets.token_hex(16),
        session_id=session_id,
        session_file=session_file,
        selected=SelectedModel(provider, model_id, context_window),
        context_tokens=context_tokens,
    )
    return await _exchange_observation(
        persistent,
        request,
        session_file,
        session_id,
        _read_settings_response,
        expected_package=expected_package,
        timeout=timeout,
        max_response=16384,
    )
