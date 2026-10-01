"""Inherited-deadline native writer transport and exact journal outcome settlement."""
from __future__ import annotations

import json
import math
import shutil
import time
from pathlib import Path

from .child_process import BoundedRun, TimedOutOutcome
from .compaction_boundary import HeldCompaction
from .compaction_journal import CompactionJournal
from .compaction_records import CompactionOperation
from .compaction_states import NativeOutcome, UnknownNativeOutcome
from .field_codec import FieldCodec
from .native_compaction_request import NativeAuthority, NativeRequest
from .native_package import COMPACTION_HELPER, verify_native_package


class CompactionTransportUnknownError(RuntimeError):
    """Native mutation may have occurred; only exact reconciliation can settle it."""


class NativeCompactionWriter:
    def __init__(self, package_dir: Path):
        BoundedRun.require_inherited_deadline()
        self.package_dir = package_dir.resolve(strict=True)
        self.helper = self.package_dir / "dist/agent-comms-compaction-commit-child.mjs"
        self.import_fence = self.package_dir / "dist/agent-comms-import-fence.mjs"
        node = shutil.which("node")
        if node is None:
            raise ValueError("Node executable unavailable")
        self.node = node
        environment_launcher = shutil.which("env")
        if environment_launcher is None:
            raise ValueError("Isolated native environment launcher unavailable")
        self.environment_launcher = environment_launcher
        self.verify()

    def verify(self) -> None:
        verify_native_package(self.package_dir)
        copied_helper = self.package_dir / "dist/agent-comms-compaction-commit-child.mjs"
        if (
            not copied_helper.is_file()
            or copied_helper.read_bytes() != COMPACTION_HELPER.read_bytes()
        ):
            raise ValueError("Trusted compaction helper differs from packaged resource")
        if not self.import_fence.is_file():
            raise ValueError("Native import boundary unavailable")


    def exchange(
        self, fd: int, request: NativeRequest, timeout: float, retained_fds: tuple[int, ...] = ()
    ) -> NativeOutcome:
        self.verify()
        encoded = FieldCodec.encode(request)
        encoded["authority"] = FieldCodec.encode(NativeAuthority.capture(fd))
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("Compaction child deadline must be in (0, 30] seconds")
        payload = json.dumps(encoded, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            result = BoundedRun.run_inherited(
                (
                    self.environment_launcher,
                    "-u",
                    "NODE_OPTIONS",
                    "-u",
                    "NODE_PATH",
                    "-u",
                    "NODE_COMPILE_CACHE",
                    "NODE_DISABLE_COMPILE_CACHE=1",
                    self.node,
                    "--no-global-search-paths",
                    "--import",
                    str(self.import_fence),
                    str(self.helper),
                    str(self.package_dir),
                    str(fd),
                    str(len(payload)),
                ),
                input=payload,
                pass_fds=tuple(dict.fromkeys((fd, *retained_fds))),
                deadline=time.monotonic() + timeout,
            )
        except (OSError, RuntimeError) as error:
            raise CompactionTransportUnknownError(
                f"Native commit transport UNKNOWN: {error}; never replay"
            ) from error
        if isinstance(result.outcome, TimedOutOutcome):
            raise CompactionTransportUnknownError("Native commit timed out; never replay")
        try:
            evidence = json.loads(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise CompactionTransportUnknownError(
                "Unparseable native outcome; never replay"
            ) from error
        try:
            return FieldCodec.decode(NativeOutcome, evidence).checked_child(result.outcome)
        except (ValueError, TypeError) as error:
            raise CompactionTransportUnknownError(str(error)) from error


    def settle(
        self, held: HeldCompaction, request: NativeRequest,
        journal: CompactionJournal, timeout: float,
    ) -> CompactionOperation:
        """One dispatch, one observed outcome; a transport fault never authorizes replay."""
        try:
            outcome = self.exchange(held.authority_fd, request, timeout, held.retained_fds)
        except Exception as error:
            outcome = UnknownNativeOutcome(str(error)[:1024])
        outcome = outcome.bind_metadata(request.commit.metadata_digest)
        journal.operations.resolve(request.commit.commit_id, outcome)
        return journal.operations.get(request.commit.commit_id)
