"""Private terminal diagnostics: structural facts only, never provider text or prompts."""

from __future__ import annotations

import json
import os
import re
from enum import StrEnum
from pathlib import Path

from .declarations import _atomic_write_text


class FailureReason(StrEnum):
    BACKEND_FAILED = "backend_failed"
    PREFLIGHT_TIMEOUT = "native_preflight_timeout"
    PREFLIGHT_EXIT = "native_preflight_exit"
    PROOF_JOURNAL_LIMIT = "native_proof_journal_limit"
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
    root: Path, *, turn_id: str, thread: str, event: dict, sequences: tuple[int, ...]
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
            "proof_journal_bytes",
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
