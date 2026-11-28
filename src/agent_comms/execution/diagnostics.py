"""Private terminal diagnostics, including correlated native RPC refusals."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from traceback import TracebackException
from typing import TYPE_CHECKING

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.execution.child_process import Platform
from agent_comms.execution.native_failures import NativeRequestFailure
from agent_comms.execution.turn_failure import TurnFailure
from agent_comms.nominal.field_codec import (
    ArtifactExclude,
    FieldCodec,
    InputId,
    ScratchName,
    TurnId,
)
from agent_comms.registry.activity import DrainDiagnostic
from agent_comms.storage.locked_store import PatternFile, RuntimeFile
from agent_comms.storage.store_files import AtomicFile

if TYPE_CHECKING:
    from agent_comms.execution.pi_events import Response
    from agent_comms.registry.threads import Thread


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
        path = cls.path_in(root, lookup=thread.incarnation.lookup, id=ScratchName.mint())
        path.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(f"Owner launch: {thread.name}\n")
            output.flush()
            yield output


def record_terminal_failure(
    root: Path,
    *,
    subject: TurnId | InputId,
    thread: str,
    failure: TurnFailure,
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
        "text": failure.text,
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
            replace(diagnostic, reason=str(source_error)), ArtifactExclude
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
