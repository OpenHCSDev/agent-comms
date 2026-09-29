"""Journaled summary exchange with the owner's already selected Pi child.

ACP activation and native RPC installation are separate. This adapter never
starts a child, resolves credentials, commits a summary, or replays input.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .backend import MODEL_WAIT_TIMEOUT_SECONDS, PersistentPiSession
from .compaction_journal import CompactionJournal, SelectedSummarySource
from .field_codec import FieldCodec
from .fresh_private_session import FreshPrivateSession
from .input_disposition import FutureInputQueue
from .native_pi import NativePiUnavailable
from .native_session_reopen import NativeSessionIdentity
from .owner_compaction_prepare import NativeWitness
from .owner_compaction_provider import NativeSummary
from .pi_commands import AgentCommsSummarizeCompaction
from .pi_events import AgentCommsCompactionProgress, Response
from .pi_rpc import PiRpcChannel
from .pi_summary_payloads import (
    SummaryDeclinedData,
    SummaryFailedData,
    SummarySummarizedData,
    SummaryUnknownData,
)


class SelectedChildUnknown(RuntimeError):  # noqa: N818 - UNKNOWN is a protocol state
    """The selected operation may have started; never replay input on uncertainty."""


class SelectedSummaryFailed(RuntimeError):  # noqa: N818 - terminal protocol state
    """Summary failed without modifying the source or admitting original input."""

    def __init__(self, receipt: SummaryFailedData):
        self.operation_id = receipt.operation_id
        self.reason = receipt.reason
        super().__init__(f"Selected summary failed: {receipt.reason}; original input not sent")


@dataclass(frozen=True)
class SelectedSummaryResult:
    operation_id: str
    summary: NativeSummary | None
    decline_reason: str | None = None


def _summary_response(
    raw: bytes, request: AgentCommsSummarizeCompaction, tokens_before: int
) -> SelectedSummaryResult | SummaryFailedData:
    """Decode the existing native v1 protocol once at the RPC boundary."""

    try:
        if not raw or not raw.endswith(b"\n"):
            raise ValueError("Incomplete selected summary")
        response = PiRpcChannel.decode_record(raw, strict=True)
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
                else (
                    "Selected summary outcome is uncertain; "
                    "native child supplied no failure detail"
                )
            )
            raise SelectedChildUnknown(detail)
        if not isinstance(data, (SummarySummarizedData, SummaryFailedData)) or (
            data.witness != request.witness
            or data.selected != request.selected
            or data.settings != request.settings
        ):
            raise ValueError("Selected summary outcome unknown")
        if isinstance(data, SummaryFailedData):
            return data
        if (
            data.result.first_kept_entry_id != request.witness.first_kept_entry_id
            or data.result.tokens_before != tokens_before
        ):
            raise ValueError("Selected summary result source changed")
        return SelectedSummaryResult(
            data.operation_id,
            NativeSummary(
                data.result.summary,
                data.result.details,
                data.result.usage,
            ),
        )
    except (ValueError, TypeError, KeyError) as error:
        raise SelectedChildUnknown(f"Selected summary response is uncertain: {error}") from error


@dataclass
class SelectedSummarySlot:
    owner: str
    session: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def run_selected_summary(
        self,
        persistent: PersistentPiSession,
        journal: CompactionJournal,
        witness: NativeWitness,
        source: dict[str, Any],
        *,
        expected_package: Path,
        tokens_before: int,
        custom_instructions: str | None = None,
        fresh_session: FreshPrivateSession | None = None,
        admission_generation: int | None = None,
        future_queue: FutureInputQueue | None = None,
        idle_timeout_seconds: float = MODEL_WAIT_TIMEOUT_SECONDS,
    ) -> SelectedSummaryResult:
        """Reserve durably, exchange once, and leave settlement to the owner.

        Caller holds owner/turn exclusion and supplies its captured source.
        Native v1 owns selected model/settings/route validation. A successful
        response is summary data, never commit or original-input authority.
        Every reservation stays blocking until the existing commit/recovery
        protocol settles it. Failure never authorizes another attempt.
        """
        envelope = FieldCodec.decode(SelectedSummarySource, source)
        source = FieldCodec.encode(envelope)
        request = AgentCommsSummarizeCompaction(
            id=secrets.token_hex(16),
            version=1,
            operation_id="",
            witness=witness,
            selected=envelope.selected,
            settings=envelope.settings,
            custom_instructions=custom_instructions,
        )
        if (
            witness.session_id != self.session
            or envelope.source.incarnation.name != self.owner
            or type(tokens_before) is not int
            or not 0 <= tokens_before <= 2**53 - 1
            or not 0 < idle_timeout_seconds < float("inf")
        ):
            raise ValueError("Exact selected owner, session and bounded deadline required")
        async with self.lock, persistent.lock:
            session_file = witness.session_file
            try:
                retained = persistent.custody.idle().selected(
                    NativeSessionIdentity(self.session, session_file), expected_package
                )
            except NativePiUnavailable as error:
                raise SelectedChildUnknown(str(error)) from error
            if witness.revision != ":".join(map(str, retained.revision[0])):
                raise SelectedChildUnknown("Selected source witness is stale")
            proc, reader = retained.child.proc, retained.child.reader
            operation = journal.reserve_selected_summary(
                session_file,
                source,
                fresh_session=fresh_session,
                admission_generation=admission_generation,
                future_queue=future_queue,
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
                        raw = await reader.readline()
                    event = PiRpcChannel.decode_record(raw, strict=True)
                    if not isinstance(event, AgentCommsCompactionProgress):
                        break
                    if event.id != request.id or event.operation_id != operation:
                        raise SelectedChildUnknown("Foreign selected compaction progress")
                    if event.sequence > sequence:
                        sequence = event.sequence
                        deadline = loop.time() + idle_timeout_seconds
                result = _summary_response(raw, request, tokens_before)
                if not retained.current:
                    raise SelectedChildUnknown("Selected source changed during summary")
                if isinstance(result, SummaryFailedData):
                    journal.fail_selected_summary(operation, result.reason)
                elif result.summary is None and result.decline_reason not in {
                    "split_turn",
                    "unsupported",
                }:
                    journal.refuse_selected_summary(operation, result.decline_reason)
            except BaseException as error:
                persistent.require_reopen(session_file)
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
            # Raise only after attestation and durable settlement succeed. This
            # known terminal outcome must not enter the transport UNKNOWN handler.
            if isinstance(result, SummaryFailedData):
                raise SelectedSummaryFailed(result)
            return result
