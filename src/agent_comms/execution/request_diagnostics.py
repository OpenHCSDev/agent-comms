"""Append-only native request and acquisition timing diagnostics."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from agent_comms.execution.child_process import ProcessState
from agent_comms.execution.publication_measurements import (
    CoordinatorRun,
    CoordinatorWait,
    PublicationMeasurements,
    ThreadCpuMeasurements,
    TransactionWait,
    WorkCpu,
)
from agent_comms.execution.request_progress import RequestProgress
from agent_comms.execution.turn_lease import TurnLeaseFence
from agent_comms.nominal.field_codec import FieldCodec, InputId, WireOmitDefault
from agent_comms.storage.locked_store import PatternFile, RuntimeFile


@dataclass(frozen=True, kw_only=True)
class RequestObservation:
    """One line of a turn's request diagnostic stream, fenced by the exact turn lease.

    It holds no prompt bodies, headers or credentials and grants no retry, send or
    replay permission. ``recorded_monotonic_ns`` is the local monotonic clock."""

    turn: TurnLeaseFence
    native_process: ProcessState
    recorded_monotonic_ns: int = field(default_factory=time.monotonic_ns)


@dataclass(frozen=True, kw_only=True)
class RequestProgressObservation(RequestObservation):
    """Native request progress; native clocks stay native."""

    native: RequestProgress
    publication_completed_cumulative: Annotated[
        PublicationMeasurements | None, WireOmitDefault()
    ] = None


@dataclass(frozen=True, kw_only=True)
class AcquisitionObservation(RequestObservation):
    """The parent's acquisition timings for one original input, after its resources close."""

    input_id: InputId
    acquisition: PublicationMeasurements
    delivery_cumulative: PublicationMeasurements
    """What the recipient's cursor delivery has timed since its session began: observing the
    cursor, accepting deliveries and the claim that precedes this input's reservation. They
    come before the input exists, so its own counters cannot hold them; the difference
    between two of a session's rows is the work between them."""
    work_cpu_cumulative: ThreadCpuMeasurements = field(default_factory=WorkCpu.totals)
    """Thread CPU this process has spent in each kind of joined work up to this turn's end."""
    coordinator_wait_cumulative: PublicationMeasurements = field(
        default_factory=CoordinatorWait.totals
    )
    """How long each kind of coordinator operation has waited for the coordinator thread."""
    coordinator_run_cumulative: PublicationMeasurements = field(
        default_factory=CoordinatorRun.totals
    )
    """How long each kind of coordinator operation has run on the coordinator thread."""
    transaction_wait_cumulative: PublicationMeasurements = field(
        default_factory=TransactionWait.totals
    )
    """How long each kind of store has spent beginning and committing its transactions."""


def record_request_progress(
    root: Path,
    lease: TurnLeaseFence,
    progress: RequestProgress,
    native_process: ProcessState,
    *,
    publication: PublicationMeasurements | None = None,
) -> None:
    """Append original measurements with the exact existing turn/owner fence.

    This private diagnostic does not contain prompt bodies, headers or credentials,
    grant retry or participate in lifecycle decisions. Native clocks remain native;
    acknowledgement and publication spans use the local monotonic clock independently.
    """
    RequestObservations.record(
        root,
        RequestProgressObservation(
            turn=lease,
            native=progress,
            native_process=native_process,
            publication_completed_cumulative=publication,
        ),
    )


def record_acquisition_progress(
    root: Path,
    lease: TurnLeaseFence,
    input_id: InputId,
    measurements: PublicationMeasurements,
    native_process: ProcessState,
    delivery: PublicationMeasurements,
) -> None:
    """Report the parent's acquisition timings after its resources close.

    The original input owns these counters, not the recipient's cursor lifetime.
    NativeStartup observed the actual acquired child, or NoProcess if acquisition
    never returned. This diagnostic grants no send, readiness or replay permission.
    """
    RequestObservations.record(
        root,
        AcquisitionObservation(
            turn=lease,
            input_id=input_id,
            acquisition=measurements,
            native_process=native_process,
            delivery_cumulative=delivery,
        ),
    )


class RequestObservations(RuntimeFile, PatternFile):
    """The original append-only request diagnostic stream and its publication."""

    filename = "diagnostics/{turn_id}.requests.jsonl"

    @classmethod
    def record(cls, root: Path, observation: RequestObservation) -> None:
        path = cls.path_in(root, turn_id=observation.turn.turn_id)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(FieldCodec.encode(observation)) + "\n")
