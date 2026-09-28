"""Journaled summary exchange with the owner's already selected Pi child.

ACP activation and native RPC installation are separate. This adapter never
starts a child, resolves credentials, commits a summary, or replays input.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .backend import MODEL_WAIT_TIMEOUT_SECONDS, PersistentPiSession, _session_revision
from .compaction_journal import CompactionJournal
from .fresh_private_session import FreshPrivateSession
from .owner_compaction_prepare import NativeWitness
from .owner_compaction_provider import NativeSummary
from .pi_commands import AgentCommsSummarizeCompaction
from .pi_events import AgentCommsCompactionProgress, Response
from .pi_rpc import PiRpcChannel
from .pi_summary_payloads import SummaryDeclinedData, SummarySummarizedData, SummaryUnknownData
from .selected_pi_child_deadline import SelectedChildUnknown, arm_selected_child
from .selected_pi_route import _request

# Native v1 text/file limits, allowing JSON's six-byte control escaping.
_MAX_RESPONSE = 6 * (262144 + 2 * 256 * 4096) + 65536


@dataclass(frozen=True)
class SelectedSummaryResult:
    operation_id: str
    summary: NativeSummary | None
    decline_reason: str | None = None


def _summary_response(
    raw: bytes, request: AgentCommsSummarizeCompaction, tokens_before: int
) -> SelectedSummaryResult:
    """Decode the existing native v1 protocol once at the RPC boundary."""

    try:
        if not raw or len(raw) > _MAX_RESPONSE or not raw.endswith(b"\n"):
            raise ValueError("Incomplete bounded selected summary")
        response = PiRpcChannel.decode_record(raw, strict=True, max_bytes=_MAX_RESPONSE)
        if (
            not isinstance(response, Response)
            or response.id != request.id
            or response.command is not AgentCommsSummarizeCompaction
            or response.success is not True
        ):
            raise ValueError("Unmatched selected summary response")
        data = response.data
        if data is None or data.operation_id != request.operation_id:
            raise ValueError("Unmatched selected summary operation")
        if isinstance(data, SummaryDeclinedData):
            return SelectedSummaryResult(data.operation_id, None, data.reason)
        if isinstance(data, SummaryUnknownData):
            detail = (
                f"Selected summary failed: {data.reason} (outcome uncertain; input not retried)"
                if data.reason is not None
                else "Selected summary outcome is uncertain; native child supplied no failure detail"
            )
            raise SelectedChildUnknown(detail)
        if not isinstance(data, SummarySummarizedData) or (
            data.witness != request.witness
            or data.selected != request.selected
            or data.settings != request.settings
            or data.result.first_kept_entry_id != request.witness.first_kept_entry_id
            or data.result.tokens_before != tokens_before
        ):
            raise ValueError("Selected summary outcome unknown")
        return SelectedSummaryResult(
            data.operation_id,
            NativeSummary(
                data.result.summary,
                data.result.details.to_wire(),
                data.result.usage.to_wire(),
            ),
        )
    except (ValueError, TypeError, KeyError) as error:
        raise SelectedChildUnknown(f"Selected summary response is uncertain: {error}") from error


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
        witness: NativeWitness,
        source: dict[str, Any],
        *,
        expected_launcher: str,
        tokens_before: int,
        custom_instructions: str | None = None,
        fresh_session: FreshPrivateSession | None = None,
        admission_generation: int | None = None,
        idle_timeout_seconds: float = MODEL_WAIT_TIMEOUT_SECONDS,
    ) -> SelectedSummaryResult:
        """Reserve durably, exchange once, and leave settlement to the owner.

        Caller holds owner/turn exclusion and supplies its captured source.
        Native v1 owns selected model/settings/route validation. A successful
        response is summary data, never commit or original-input authority.
        Every reservation stays blocking until the existing commit/recovery
        protocol settles it. No automatic fallback follows any failure.
        """
        source = json.loads(json.dumps(source, allow_nan=False))
        preparation = _request(witness, source["selected"], source["settings"])
        request = AgentCommsSummarizeCompaction(
            id=preparation.id,
            version=1,
            operation_id="",
            witness=witness,
            selected=preparation.selected,
            settings=preparation.settings,
            custom_instructions=custom_instructions,
        )
        if (
            witness.session_id != self.session
            or source["source"].get("ownerName") != self.owner
            or type(tokens_before) is not int
            or not 0 <= tokens_before <= 2**53 - 1
            or not expected_launcher
            or not 0 < idle_timeout_seconds < float("inf")
        ):
            raise ValueError("Exact selected owner, session and bounded deadline required")
        async with self.lock, persistent.lock:
            proc, reader = persistent.proc, persistent.reader
            session_file = witness.session_file
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
                or witness.revision != ":".join(map(str, revision[0]))
                or persistent.launch_key is None
                or persistent.launch_key[0] != expected_launcher
            ):
                raise SelectedChildUnknown("Selected idle Pi child is unavailable or stale")
            operation = journal.reserve_selected_summary(
                session_file,
                source,
                fresh_session=fresh_session,
                admission_generation=admission_generation,
            )
            request = replace(request, operation_id=operation)
            # The durable reservation blocks new inputs even after process
            # death; retain it for exact commit linkage on a complete result.
            try:
                proc.stdin.write(PiRpcChannel.command_bytes(request))
                loop = asyncio.get_running_loop()
                deadline = loop.time() + idle_timeout_seconds
                sequence = 0
                async with asyncio.timeout_at(deadline):
                    await proc.stdin.drain()
                while True:
                    async with asyncio.timeout_at(deadline):
                        raw = await reader.readline(max_bytes=_MAX_RESPONSE)
                    event = PiRpcChannel.decode_record(raw, strict=True, max_bytes=_MAX_RESPONSE)
                    if not isinstance(event, AgentCommsCompactionProgress):
                        break
                    if event.id != request.id or event.operation_id != operation:
                        raise SelectedChildUnknown("Foreign selected compaction progress")
                    if event.sequence > sequence:
                        sequence = event.sequence
                        deadline = loop.time() + idle_timeout_seconds
                result = _summary_response(raw, request, tokens_before)
                if proc.returncode is not None or _session_revision(session_file) != revision:
                    raise SelectedChildUnknown("Selected source changed during summary")
                if result.summary is None and result.decline_reason not in {
                    "split_turn",
                    "unsupported",
                }:
                    journal.refuse_selected_summary(operation, result.decline_reason)
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
                if isinstance(error, TimeoutError):
                    raise SelectedChildUnknown(
                        f"Selected summary made no progress for {idle_timeout_seconds:g} seconds; "
                        "outcome uncertain, input not retried"
                    ) from error
                raise SelectedChildUnknown(
                    f"Selected summary transport uncertain: {type(error).__name__}: "
                    f"{str(error)[:1024]}"
                ) from error

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
