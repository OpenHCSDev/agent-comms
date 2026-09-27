"""Journaled summary exchange with the owner's already selected Pi child.

ACP activation and native RPC installation are separate. This adapter never
starts a child, resolves credentials, commits a summary, or replays input.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backend import PersistentPiSession, _session_revision
from .compaction_journal import CompactionJournal
from .fresh_private_session import FreshPrivateSession
from .owner_compaction_provider import NativeSummary, valid_native_usage
from .pi_rpc import PiRpcChannel
from .selected_pi_child_deadline import SelectedChildUnknown, arm_selected_child
from .selected_pi_route import _request, _strict_echo

# Native v1 text/file limits, allowing JSON's six-byte control escaping.
_MAX_RESPONSE = 6 * (262144 + 2 * 256 * 4096) + 65536


@dataclass(frozen=True)
class SelectedSummaryResult:
    operation_id: str
    summary: NativeSummary | None
    decline_reason: str | None = None


def _summary_response(
    raw: bytes, request: dict[str, Any], tokens_before: int
) -> SelectedSummaryResult:
    """Decode the existing native v1 protocol once at the RPC boundary."""

    try:
        if not raw or len(raw) > _MAX_RESPONSE or not raw.endswith(b"\n"):
            raise ValueError("Incomplete bounded selected summary")
        response = PiRpcChannel.decode_record(raw, strict=True, max_bytes=_MAX_RESPONSE).wire
        if (
            set(response) != {"id", "type", "command", "success", "data"}
            or response["id"] != request["id"]
            or response["type"] != "response"
            or response["command"] != request["type"]
            or response["success"] is not True
        ):
            raise ValueError("Unmatched selected summary response")
        data = response["data"]
        if (
            type(data) is not dict
            or type(data.get("version")) is not int
            or data["version"] != 1
            or data.get("operationId") != request["operationId"]
        ):
            raise ValueError("Unmatched selected summary operation")
        if (
            set(data) == {"version", "status", "operationId", "reason"}
            and data["status"] == "declined"
            and type(data["reason"]) is str
            and 0 < len(data["reason"]) <= 256
        ):
            # Observation only: do not issue an original-input admission or
            # clear the reservation from an RPC response alone.
            return SelectedSummaryResult(request["operationId"], None, data["reason"])
        if (
            set(data)
            != {"version", "status", "operationId", "witness", "selected", "settings", "result"}
            or data["status"] != "summarized"
            or not _strict_echo(data, request)
        ):
            raise ValueError("Selected summary outcome unknown")
        result = data["result"]
        if (
            type(result) is not dict
            or set(result) != {"summary", "firstKeptEntryId", "tokensBefore", "details", "usage"}
            or result["firstKeptEntryId"] != request["witness"]["firstKeptEntryId"]
            or type(result["tokensBefore"]) is not int
            or result["tokensBefore"] != tokens_before
            or type(result["summary"]) is not str
            or not result["summary"].strip()
            or len(result["summary"].encode()) > 262144
            or not valid_native_usage(result["usage"])
        ):
            raise ValueError("Invalid selected native summary")
        details = result["details"]
        if (
            type(details) is not dict
            or set(details) != {"readFiles", "modifiedFiles"}
            or any(
                type(paths) is not list
                or len(paths) > 256
                or any(
                    type(path) is not str or not path or "\0" in path or len(path.encode()) > 4096
                    for path in paths
                )
                for paths in details.values()
            )
        ):
            raise ValueError("Invalid selected native file operations")
        return SelectedSummaryResult(
            request["operationId"], NativeSummary(result["summary"], details, result["usage"])
        )
    except (ValueError, TypeError, KeyError) as error:
        raise SelectedChildUnknown("Selected summary response is uncertain") from error


@dataclass
class SelectedSummarySlot:
    owner: str
    session: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _attempt: asyncio.Task[dict[str, Any]] | None = field(default=None, init=False)

    async def run_selected_summary(
        self,
        persistent: PersistentPiSession,
        journal: CompactionJournal,
        witness: dict[str, Any],
        source: dict[str, Any],
        *,
        expected_launcher: str,
        tokens_before: int,
        fresh_session: FreshPrivateSession | None = None,
        admission_epoch: int | None = None,
        timeout_seconds: float = 90.0,
    ) -> SelectedSummaryResult:
        """Reserve durably, exchange once, and leave settlement to the owner.

        Caller holds owner/turn exclusion and supplies its captured source.
        Native v1 owns selected model/settings/route validation. A successful
        response is summary data, never commit or original-input authority.
        Every reservation stays blocking until the existing commit/recovery
        protocol settles it. No automatic fallback follows any failure.
        """
        source = json.loads(json.dumps(source, allow_nan=False))
        request = _request(witness, source["selected"], source["settings"])
        witness = request["witness"]
        request.pop("dryRun")
        request["type"] = "agent_comms_summarize_compaction"
        if (
            witness["sessionId"] != self.session
            or source["source"].get("ownerName") != self.owner
            or type(tokens_before) is not int
            or not 0 <= tokens_before <= 2**53 - 1
            or not expected_launcher
            or not 0 < timeout_seconds <= 90
        ):
            raise ValueError("Exact selected owner, session and bounded deadline required")
        async with self.lock, persistent.lock:
            proc, reader = persistent.proc, persistent.reader
            session_file = witness["sessionFile"]
            revision = _session_revision(session_file)
            if (
                persistent.reopen_required is not None
                or proc is None
                or proc.returncode is not None
                or proc.stdin is None
                or reader is None
                or persistent.session_file != session_file
                or persistent.session_id != self.session
                or revision is None
                or persistent.revision != revision
                or witness["revision"] != ":".join(map(str, revision[0]))
                or persistent.launch_key is None
                or persistent.launch_key[0] != expected_launcher
            ):
                raise SelectedChildUnknown("Selected idle Pi child is unavailable or stale")
            operation = journal.reserve_selected_summary(
                session_file, source, fresh_session=fresh_session, admission_epoch=admission_epoch
            )
            request["operationId"] = operation
            # The durable reservation blocks new inputs even after process
            # death; retain it for exact commit linkage on a complete result.
            try:
                proc.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
                async with asyncio.timeout(timeout_seconds):
                    await proc.stdin.drain()
                    raw = await reader.readline(max_bytes=_MAX_RESPONSE)
                result = _summary_response(raw, request, tokens_before)
                if proc.returncode is not None or _session_revision(session_file) != revision:
                    raise SelectedChildUnknown("Selected source changed during summary")
                return result
            except BaseException as error:
                persistent.reopen_required = session_file
                persistent.reopen_session_id = self.session
                # Keep the child marked unusable even if cancellation interrupts
                # its reap. PersistentPiSession owns the shielded close task.
                try:
                    journal.mark_selected_summary_unknown(operation)
                finally:
                    with suppress(asyncio.CancelledError):
                        await persistent.close()
                if isinstance(error, (asyncio.CancelledError, SelectedChildUnknown)):
                    raise
                raise SelectedChildUnknown("Selected summary transport uncertain") from error

    async def exchange_fake_rpc(
        self,
        *,
        operation: str,
        command: tuple[str, ...],
        receipt: Path,
        timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        """Provider-free single fake RPC; cancellation cannot free an active slot.

        No user input is accepted by this API. Operation ID is echoed verbatim,
        and the dedicated namespace is retired/joined even on malformed output.
        This is not a production model-attestation grant.
        """
        if not 0 < timeout_seconds <= 5 or not operation or len(operation) > 256:
            raise ValueError("Bounded fake selected operation required")
        if self._attempt is not None and not self._attempt.done():
            raise SelectedChildUnknown("Previous selected operation is still retiring")
        task = asyncio.create_task(self._run_fake(operation, command, receipt, timeout_seconds))
        self._attempt = task
        # A cancelled waiter must not cancel retirement or leak an unobserved
        # background exception. The retained task remains inspectable.
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)

    async def _run_fake(
        self,
        operation: str,
        command: tuple[str, ...],
        receipt: Path,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        async with self.lock:
            identity = await arm_selected_child(
                owner=self.owner,
                session=self.session,
                operation=operation,
                command=command,
                deadline_ns=time.monotonic_ns() + int(timeout_seconds * 1e9),
                receipt=receipt,
            )
            try:
                raw = await identity.send(
                    (
                        json.dumps(
                            {
                                "type": "selected_fake",
                                "owner": self.owner,
                                "session": self.session,
                                "operation": operation,
                                "incarnation": identity.identity.token,
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode()
                )
                response = json.loads(raw)
                if (
                    type(response) is not dict
                    or set(response)
                    != {"type", "owner", "session", "operation", "incarnation", "ok"}
                    or response
                    != {
                        "type": "selected_fake_response",
                        "owner": self.owner,
                        "session": self.session,
                        "operation": operation,
                        "incarnation": identity.identity.token,
                        "ok": True,
                    }
                ):
                    raise SelectedChildUnknown("Unbound fake selected response")
                return response
            except (ValueError, UnicodeError) as error:
                raise SelectedChildUnknown("Malformed fake selected response") from error
            finally:
                # Task, not its waiting caller, owns the slot through join.
                await identity.retire()
