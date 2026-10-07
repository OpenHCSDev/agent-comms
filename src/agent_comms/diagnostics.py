"""Private terminal diagnostics, including correlated native RPC refusals."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from traceback import TracebackException
from typing import TYPE_CHECKING
from uuid import uuid4

from .store_files import _atomic_write_text
from .field_codec import FieldCodec

_LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .activity import DrainDiagnostic
    from .pi_events import Response
    from .threads import Thread


@contextmanager
def owner_process_output(root: Path, thread: Thread):
    """Retain a detached owner's startup traceback in the existing diagnostics.

    The child inherits this descriptor; closing the parent's copy does not end
    capture. Each launch gets its own private file, without changing thread or
    input disposition state.
    """
    from .bus_publication import stable_thread_lookup

    directory = root / "diagnostics"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"owner-{stable_thread_lookup(thread.created_at)}-{uuid4().hex}.log"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(f"Owner launch: {thread.name}\n")
        output.flush()
        yield output


class FailureReason(StrEnum):
    BACKEND_FAILED = "backend_failed"
    PREFLIGHT_TIMEOUT = "native_preflight_timeout"
    PREFLIGHT_EXIT = "native_preflight_exit"
    INPUT_ID_UNAVAILABLE = "pi_input_id_unavailable"
    COMPACTION_FAILED = "prestart_compaction_failed"
    IDENTITY_UNCERTAIN = "session_identity_uncertain"
    AUTHORITY_CHANGED = "input_authority_changed"
    FOLLOWUP_UNRECOGNIZED = "unrecognized_followup_input"
    INPUT_MISSING = "current_prompt_input_missing"
    MODEL_REQUEST_FAILED = "model_request_failed"
    FINAL_STOP_MISSING = "assistant_final_stop_missing"
    QUEUED_INPUT_MISSING = "queued_input_start_missing"


@dataclass
class PublicationMeasurements:
    """Bounded transport timing counters, not a message or phase authority."""
    count: int = 0
    total_ns: int = 0
    maximum_ns: int = 0
    maximum_started_ns: int = 0
    maximum_finished_ns: int = 0
    operations: dict[str, PublicationMeasurements] = field(default_factory=dict)

    def operation(self, name: str):
        """Borrow bounded counters for a declared acquisition/publication operation.

        These counters observe resources; they never decide readiness, receipt
        acceptance or replay. Owners call this with their fixed operation names.
        """
        return self.operations.setdefault(name, PublicationMeasurements()).measuring()

    @contextmanager
    def measuring(self):
        started = time.monotonic_ns()
        try:
            yield
        finally:
            finished = time.monotonic_ns()
            duration = finished - started
            self.count += 1
            self.total_ns += duration
            if duration > self.maximum_ns:
                self.maximum_ns = duration
                self.maximum_started_ns, self.maximum_finished_ns = started, finished


def record_request_progress(root, lease, progress, native_process, summary_operation=None,
                            *, publication=None):
    """Append original measurements with the exact existing turn/owner fence.

    This private diagnostic does not contain prompt bodies, headers or credentials,
    grant retry or participate in lifecycle decisions. Native clocks remain native;
    receipt and publication spans use the local monotonic clock independently.
    """
    now = time.monotonic_ns()
    record = {"turn": FieldCodec.encode(lease), "native": FieldCodec.encode(progress),
              "native_process": FieldCodec.encode(native_process),
              "recorded_monotonic_ns": now}
    if publication is not None:
        record["publication_completed_cumulative"] = FieldCodec.encode(publication)
    if summary_operation is not None:
        record["selected_summary"] = FieldCodec.encode(summary_operation)
    _record_request_observation(root, lease, record)


def record_acquisition_progress(root, lease, input_id, measurements):
    """Publish the original parent acquisition spans after its custody closes.

    The same diagnostic stream retains native and parent clocks with distinct
    keys. There is no additional phase, readiness ledger or observation store.
    """
    _record_request_observation(root, lease, {
        "turn": FieldCodec.encode(lease), "input_id": input_id,
        "acquisition": FieldCodec.encode(measurements),
        "recorded_monotonic_ns": time.monotonic_ns(),
    })


def request_observation_path(root, turn_id):
    """Location of the original diagnostic publication, not another record."""
    return root / "diagnostics" / f"{turn_id}.requests.jsonl"


def _record_request_observation(root, lease, record):
    try:
        path = request_observation_path(root, lease.turn_id)
        directory = path.parent
        directory.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(record) + "\n")
    except OSError:
        # Optional observation cannot change an admitted original's outcome.
        _LOG.warning("Native request timing diagnostic unavailable", exc_info=True)


def terminal_failure_reason(event: dict) -> FailureReason:
    """Allowlisted backend terminal classification; never diagnostic prose."""
    measurements = event.get("diagnostic", {})
    if not isinstance(measurements, dict):
        measurements = {}
    try:
        value = measurements.get("reason") or event.get("reason_code")
        return FailureReason(value) if isinstance(value, str) else FailureReason.BACKEND_FAILED
    except (ValueError, TypeError):
        return FailureReason.BACKEND_FAILED


def record_terminal_failure(
    root: Path,
    *,
    turn_id: str,
    thread: str,
    event: dict,
    sequences: tuple[int, ...],
    native_response: Response | None = None,
    source_error: BaseException | None = None,
) -> Path:
    """Persist before publishing the failure notice; this record grants no retry authority."""
    if not re.fullmatch(r"[0-9a-f]{32}", turn_id):
        raise ValueError("Invalid diagnostic turn identity")
    measurements = event.get("diagnostic", {})
    if not isinstance(measurements, dict):
        measurements = {}
    reason = terminal_failure_reason(event)
    safe = {
        key: value
        for key in (
            "elapsed_ms",
            "wait_ms",
            "spawn_ms",
            "session_bytes",
            "exit_code",
        )
        if type(value := measurements.get(key)) is int
    }
    document = {
        "version": 1,
        "turn_id": turn_id,
        "thread": thread,
        "reason": reason.value,
        "sequences": list(sequences),
        "measurements": safe,
        "outcome": "failed; inputs must not be replayed automatically",
    }
    if native_response is not None:
        document["native_response"] = native_response.rejection_details()
    document.update(_source_error_evidence(source_error))
    return _write_diagnostic_document(root, turn_id, document)


def record_drain_failure(
    root: Path, *, thread: str, diagnostic: DrainDiagnostic, source_error: BaseException,
) -> Path:
    """Retain the original observation error before its owner publishes unavailability.

    Identical observations share a file; a different chained refusal gets its
    own immutable reference even when its outer type and message are unchanged.
    No turn, summary attempt or input disposition is fabricated here.
    """
    document = {
        "version": 1,
        "thread": thread,
        # The artifact owns the exception; activity reason is only presentation.
        # Derive the original outer reason from that exception, never parse a
        # file reference out of a previously presented diagnostic.
        "drain": FieldCodec.encode(replace(diagnostic, reason=str(source_error))),
        "outcome": "inbox observation failed; this record grants no retry authority",
        **_source_error_evidence(source_error),
    }
    digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    path = root / "diagnostics" / f"drain-{digest}.json"
    try:
        original = path.read_text()
    except FileNotFoundError:
        return _write_diagnostic_document(root, f"drain-{digest}", document)
    if original != json.dumps(document, indent=2) + "\n":
        raise ValueError("Original drain diagnostic differs from its failure document")
    return path.resolve()


def _source_error_evidence(source_error: BaseException | None) -> dict:
    if source_error is None:
        return {}
    document = {"source_error": "".join(
        TracebackException.from_exception(source_error, capture_locals=False).format(chain=True)
    )}
    from .native_pi import NativePiUnavailable

    if isinstance(source_error, NativePiUnavailable):
        document["native"] = source_error.diagnostic_evidence
    return document


def _write_diagnostic_document(root: Path, name: str, document: dict) -> Path:
    directory = root / "diagnostics"
    directory.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    path = directory / f"{name}.json"
    _atomic_write_text(path, json.dumps(document, indent=2) + "\n", fsync_parent=True)
    return path.resolve()
