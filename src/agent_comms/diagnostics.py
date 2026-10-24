"""Private terminal diagnostics, including correlated native RPC refusals."""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from traceback import TracebackException
from typing import TYPE_CHECKING
from uuid import uuid4


from .child_process import Platform
from .field_codec import FieldCodec, InputId, TurnId
from .locked_store import PatternFile, RuntimeFile
from .native_failures import NativeRequestFailure
from .mro_dispatch import MroDispatch, handles
from .store_files import AtomicFile
from .turn_failure import TurnFailure

if TYPE_CHECKING:
    from .activity import DrainDiagnostic
    from .pi_events import Response
    from .threads import Thread


class OwnerProcessOutput(RuntimeFile, PatternFile):
    """Startup output retained by the owner launcher."""

    filename = "diagnostics/owner-{lookup}-{id}.log"

    @classmethod
    @contextmanager
    def open(cls, root: Path, thread: Thread):
        """Retain a detached owner's startup traceback in the existing diagnostics.

        The child inherits this descriptor; closing the parent's copy does not end
        capture. Each launch gets its own private file, without changing thread or
        input disposition state.
        """
        path = cls.path_in(root, lookup=thread.incarnation.lookup, id=uuid4().hex)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(f"Owner launch: {thread.name}\n")
            output.flush()
            yield output


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

        These counters observe resources; they never decide readiness, acknowledgement
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


def record_request_progress(root, lease, progress, native_process, *, publication=None):
    """Append original measurements with the exact existing turn/owner fence.

    This private diagnostic does not contain prompt bodies, headers or credentials,
    grant retry or participate in lifecycle decisions. Native clocks remain native;
    acknowledgement and publication spans use the local monotonic clock independently.
    """
    now = time.monotonic_ns()
    record = {"turn": FieldCodec.encode(lease), "native": FieldCodec.encode(progress),
              "native_process": FieldCodec.encode(native_process),
              "recorded_monotonic_ns": now}
    if publication is not None:
        record["publication_completed_cumulative"] = FieldCodec.encode(publication)
    RequestObservations.record(root, lease, record)


def record_acquisition_progress(root, lease, input_id, measurements):
    """Report the parent's acquisition timings after its resources close.

    The same diagnostic stream retains native and parent clocks with distinct
    keys. There is no additional phase, readiness ledger or observation store.
    """
    RequestObservations.record(root, lease, {
        "turn": FieldCodec.encode(lease), "input_id": input_id,
        "acquisition": FieldCodec.encode(measurements),
        "recorded_monotonic_ns": time.monotonic_ns(),
    })


class RequestObservations(RuntimeFile, PatternFile):
    """The original append-only request diagnostic stream and its publication."""

    filename = "diagnostics/{turn_id}.requests.jsonl"

    @classmethod
    def record(cls, root, lease, record):
        path = cls.path_in(root, turn_id=lease.turn_id)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(record) + "\n")


def record_terminal_failure(
    root: Path,
    *,
    subject: TurnId | InputId,
    thread: str,
    failure: type[TurnFailure],
    sequences: tuple[int, ...],
    measurements: Mapping[str, object] | None = None,
    native_response: Response | None = None,
    source_error: BaseException | None = None,
) -> Path:
    """Persist before publishing the failure notice; this record grants no retry authority."""
    measurements = measurements or {}
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
        "subject": subject,
        "thread": thread,
        "failure": failure.declared_name,
        "sequences": list(sequences),
        "measurements": safe,
        "outcome": "failed; inputs must not be replayed automatically",
    }
    if native_response is not None:
        document["native_response"] = native_response.rejection_details()
    document.update(DiagnosticDocument.source_error_evidence(source_error))
    return DiagnosticDocument.write(root, subject, document)


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
        # file reference out of a previously presented diagnostic. The declared
        # artifact view excludes the activity-owned observation clock.
        "drain": FieldCodec.project(
            replace(diagnostic, reason=str(source_error)), "artifact"
        ),
        "outcome": "inbox observation failed; this record grants no retry authority",
        **DiagnosticDocument.source_error_evidence(source_error),
    }
    digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    path = DiagnosticDocument.path_in(root, name=f"drain-{digest}")
    try:
        original = path.read_text()
    except FileNotFoundError:
        return DiagnosticDocument.write(root, f"drain-{digest}", document)
    if original != json.dumps(document, indent=2) + "\n":
        raise ValueError("Original drain diagnostic differs from its failure document")
    return path.resolve()


class DiagnosticDocument(RuntimeFile, PatternFile, MroDispatch):
    """Publish diagnostic documents through their single declared location."""

    filename = "diagnostics/{name}.json"

    @classmethod
    def source_error_evidence(cls, source_error: BaseException | None) -> dict:
        if source_error is None:
            return {}
        document = {"source_error": "".join(
            TracebackException.from_exception(source_error, capture_locals=False).format(chain=True)
        )}
        document.update(cls.handler_for(type(source_error))(source_error))
        return document

    @handles(NativeRequestFailure)
    @staticmethod
    def native_evidence(error: NativeRequestFailure) -> dict:
        return {"native": error.diagnostic_evidence()}

    @handles(BaseException)
    @staticmethod
    def ordinary_evidence(error: BaseException) -> dict:
        return {}

    @classmethod
    def write(cls, root: Path, name: str, document: dict) -> Path:
        path = cls.path_in(root, name=name)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        Platform.current().sync_directory(root)
        AtomicFile(path).write_text(json.dumps(document, indent=2) + "\n")
        return path.resolve()
