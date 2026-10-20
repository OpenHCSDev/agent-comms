"""Capture an existing worker's original typed launch and process identity."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .child_process import Platform, ProcessIdentity
from .errors import RelationViolationError
from .launch_runtime import LaunchRecord
from .registry_document import RegistrySnapshot
from .threads import Thread


@dataclass(frozen=True, slots=True)
class RetainedOwnerLaunch:
    """One checked process observation and the record it actually launched with."""

    process: ProcessIdentity
    record: LaunchRecord
    environment: dict[str, str] = field(repr=False)

    @classmethod
    def capture(
        cls, thread: Thread, snapshot: RegistrySnapshot,
        *, interpreter: str | None = None,
    ) -> RetainedOwnerLaunch:
        process = thread.require_process()
        platform = Platform.current()
        platform.require(process)
        record = LaunchRecord.for_process(process.pid)
        observed = os.fsdecode(Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0", 1)[0])
        if observed != str(record.runtime.interpreter):
            raise RelationViolationError("Selected source owner interpreter changed")
        if interpreter is not None and observed != interpreter:
            raise RelationViolationError("Selected source owner interpreter changed")
        raw = Path(f"/proc/{process.pid}/environ").read_bytes()
        values = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        environment = {os.fsdecode(key): os.fsdecode(value) for key, value in values.items()}
        platform.require(process)
        route = record.require_thread().name
        # Rename authority comes from the captured registry incarnation, not spelling or PID.
        if snapshot.owner_identity(route) != snapshot.owner_identity(thread.name):
            raise RelationViolationError("Owner launch names another registry incarnation")
        snapshot.require_owner_process(snapshot.owner_identity(thread.name), process)
        return cls(process, record, environment)
