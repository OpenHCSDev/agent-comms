"""Private terminal diagnostics, including correlated native RPC refusals."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from traceback import TracebackException
from typing import TYPE_CHECKING
from uuid import uuid4

from .store_files import _atomic_write_text

if TYPE_CHECKING:
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
    FINAL_STOP_MISSING = "assistant_final_stop_missing"
    QUEUED_INPUT_MISSING = "queued_input_start_missing"


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
    if source_error is not None:
        document["source_error"] = "".join(
            TracebackException.from_exception(source_error, capture_locals=False).format(chain=True)
        )
    directory = root / "diagnostics"
    directory.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    path = directory / f"{turn_id}.json"
    _atomic_write_text(path, json.dumps(document, indent=2) + "\n", fsync_parent=True)
    return path.resolve()
