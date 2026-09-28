"""Provider-free probe of the already selected idle Pi RPC child.

This v1 dry-run response is NOT credential/header route attestation, a summary,
or commit/input authority. The patched native command is not enabled by ACP
until its exact bytes receive separate review. No subprocess is started here.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from .backend import PersistentPiSession, _session_revision
from .field_codec import FieldCodec
from .owner_compaction_prepare import NativeWitness
from .owner_compaction_settings import PiCompactionDecision
from .pi_rpc import PiRpcChannel

_COMMAND = "agent_comms_prepare_compaction"
_REASONS = frozenset(
    {
        "busy",
        "queue_nonempty",
        "compacting",
        "source_mismatch",
        "model_mismatch",
        "settings_mismatch",
        "split_turn",
        "unsupported",
    }
)


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
) -> dict[str, Any]:
    if (
        type(selected) is not dict
        or set(selected) != {"provider", "modelId", "contextWindow"}
        or any(
            type(selected[key]) is not str or not selected[key] for key in ("provider", "modelId")
        )
        or type(selected["contextWindow"]) is not int
        or not 0 < selected["contextWindow"] <= 2**53 - 1
        or type(settings) is not dict
        or set(settings) != {"reserveTokens", "keepRecentTokens"}
        or any(type(settings[key]) is not int for key in settings)
        or not 0 <= settings["reserveTokens"] <= 10_000_000
        or not 0 < settings["keepRecentTokens"] <= 10_000_000
    ):
        raise ValueError("Exact bounded selected Pi dry-run request required")
    return {
        "id": secrets.token_hex(16),
        "type": _COMMAND,
        "version": 1,
        "dryRun": True,
        "witness": FieldCodec.encode(witness),
        "selected": dict(selected),
        "settings": dict(settings),
    }


def _strict_echo(data: dict[str, Any], request: dict[str, Any]) -> bool:
    witness, selected, settings = (
        data.get("witness"),
        data.get("selected"),
        data.get("settings"),
    )
    return (
        FieldCodec.encode(FieldCodec.decode(NativeWitness, witness)) == request["witness"]
        and type(selected) is dict
        and type(settings) is dict
        and set(selected) == set(request["selected"])
        and set(settings) == set(request["settings"])
        and all(type(selected[key]) is str for key in ("provider", "modelId"))
        and type(selected["contextWindow"]) is int
        and all(type(value) is int for value in settings.values())
        and selected == request["selected"]
        and settings == request["settings"]
    )


def _read_response(raw: bytes, request: dict[str, Any]) -> SelectedPiDryRun:
    if not raw or len(raw) > 8192 or not raw.endswith(b"\n"):
        raise SelectedPiProbeUnknownError("Incomplete bounded selected Pi response")
    try:
        response = PiRpcChannel.decode_record(raw).wire
    except (UnicodeError, ValueError) as error:
        raise SelectedPiProbeUnknownError("Invalid selected Pi response") from error
    if (
        set(response) != {"id", "type", "command", "success", "data"}
        or response["id"] != request["id"]
        or response["type"] != "response"
        or response["command"] != _COMMAND
        or response["success"] is not True
        or type(response["data"]) is not dict
    ):
        raise SelectedPiProbeUnknownError("Unmatched selected Pi response")
    data = response["data"]
    if (
        set(data) == {"version", "status", "routeStatus", "witness", "selected", "settings"}
        and type(data["version"]) is int
        and data["version"] == 1
        and data["status"] == "ready"
        and data["routeStatus"] == "UNVERIFIED_NO_AUTH_RESOLUTION"
        and _strict_echo(data, request)
    ):
        return SelectedPiDryRun("ready", None, "UNVERIFIED_NO_AUTH_RESOLUTION")
    if (
        set(data) == {"version", "status", "reason"}
        and type(data["version"]) is int
        and data["version"] == 1
        and data["status"] == "declined"
        and type(data["reason"]) is str
        and data["reason"] in _REASONS
    ):
        return SelectedPiDryRun("declined", data["reason"], None)
    raise SelectedPiProbeUnknownError("Unrecognized selected Pi dry-run outcome")


async def probe_idle_selected_pi(
    persistent: PersistentPiSession,
    witness: NativeWitness,
    selected: dict[str, Any],
    settings: dict[str, Any],
    *,
    expected_launcher: str,
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
        expected_launcher=expected_launcher,
        timeout=timeout,
    )


_Observation = TypeVar("_Observation")


async def _exchange_observation(
    persistent: PersistentPiSession,
    request: dict[str, Any],
    session_file: str,
    session_id: str,
    decode: Callable[[bytes, dict[str, Any]], _Observation],
    *,
    expected_launcher: str,
    timeout: float,
    max_response: int | None = None,
) -> _Observation:
    """One read-only request; every uncertain transport retires the borrowed child."""
    if type(expected_launcher) is not str or not expected_launcher or not 0 < timeout <= 5:
        raise ValueError("Bounded selected Pi launcher and deadline required")
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
            or persistent.launch_key[0] != expected_launcher
        ):
            raise SelectedPiProbeUnknownError("Selected idle Pi child is unavailable or stale")
        transmitted = False
        try:
            proc.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
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


def _read_settings_response(raw: bytes, request: dict[str, Any]) -> PiCompactionDecision:
    if not raw.endswith(b"\n") or len(raw) > 16384:
        raise SelectedPiProbeUnknownError("Incomplete selected settings response")
    response = PiRpcChannel.decode_record(raw, strict=True, max_bytes=16384).wire
    if (
        set(response) != {"id", "type", "command", "success", "data"}
        or response["id"] != request["id"]
        or response["type"] != "response"
        or response["command"] != request["type"]
        or response["success"] is not True
    ):
        raise SelectedPiProbeUnknownError("Unmatched selected settings response")
    data = response["data"]
    if (
        type(data) is not dict
        or set(data)
        != {"version", "sessionId", "sessionFile", "selected", "contextTokens", "decision"}
        or type(data["version"]) is not int
        or data["version"] != 1
        or type(data["contextTokens"]) is not int
        or type(data["selected"]) is not dict
        or type(data["selected"].get("contextWindow")) is not int
        or any(
            data[key] != request[key]
            for key in ("sessionId", "sessionFile", "selected", "contextTokens")
        )
    ):
        raise SelectedPiProbeUnknownError("Selected settings source changed")
    return PiCompactionDecision.from_native(data["decision"])


async def read_selected_compaction_decision(
    persistent: PersistentPiSession,
    *,
    session_file: str,
    expected_launcher: str,
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
    request = {
        "id": secrets.token_hex(16),
        "type": "agent_comms_compaction_settings",
        "version": 1,
        "sessionId": session_id,
        "sessionFile": session_file,
        "selected": {"provider": provider, "modelId": model_id, "contextWindow": context_window},
        "contextTokens": context_tokens,
    }
    return await _exchange_observation(
        persistent,
        request,
        session_file,
        session_id,
        _read_settings_response,
        expected_launcher=expected_launcher,
        timeout=timeout,
        max_response=16384,
    )
