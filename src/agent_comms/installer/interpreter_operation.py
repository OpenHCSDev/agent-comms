"""Declared operations sent to an interpreter, with answers separate from diagnostics.

Runtime probes execute this file by path with ``python -P``. The selected runtime
supplies its own imports even when it predates this file.
"""

from __future__ import annotations

import base64
import inspect
import os
import shlex
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import ClassVar, cast
from zipfile import ZIP_DEFLATED, ZipFile

from typing_extensions import TypeForm

from agent_comms.child_process import Platform
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, JsonValue
from agent_comms.maintenance_barrier import HOLDER_NONCE


@dataclass(frozen=True)
class InstallPhase(DeclaredFamily, affix="Phase"):
    """A declared operation measures its own run through the install's shared clock."""

    def label(self) -> str:
        return type(self).__name__

    def launched(self) -> LaunchedPhase:
        return LaunchedPhase(type(self))

    def started_at(self, now: float) -> float:
        return now

    @contextmanager
    def running(self, clock: InstallClock) -> Iterator[PhaseTiming]:
        with clock.lock:
            now = clock.advance()
            started = self.started_at(now)
            measured = PhaseTiming(self, seconds=now - started)
            clock.phases.append(measured)
            clock.active.append(measured)
        try:
            yield measured
        finally:
            with clock.lock:
                measured.elapsed_seconds = clock.advance() - started
                clock.active[:] = [active for active in clock.active if active is not measured]


class SubjectPhase(InstallPhase):
    """A phase derives its subject from the typed operation it measures."""

    @property
    @abstractmethod
    def subject(self) -> str: ...

    def label(self) -> str:
        return f"{type(self).__name__}: {self.subject}"

    def launched(self) -> LaunchedPhase:
        return LaunchedPhase(type(self), self.subject)


class InstallRunPhase(InstallPhase):
    """The whole installer, including teardown and its final publication."""


class LauncherPhase(InstallPhase):
    """The launcher's real lifetime starts before Python imports its CLI."""

    def started_at(self, now: float) -> float:
        platform = Platform.current()
        return platform.started_at(platform.identity(os.getpid()))


class PreparePhase(InstallPhase):
    """Read the selected target, outgoing runtime and pending operation declarations."""


class TargetCheckoutPreparePhase(PreparePhase):
    """Prepare the target revision's checkout."""


class LiveCheckoutPreparePhase(PreparePhase):
    """Advance and prepare the live checkout."""


class RestoredCheckoutPreparePhase(PreparePhase):
    """Restore the outgoing checkout after an install failure."""


class BuildPhase(InstallPhase):
    """Resolve and build the target stack and its native package."""


@dataclass(frozen=True)
class CommandPhase(SubjectPhase):
    """An external command, identified by its actual invocation."""

    command: tuple[str, ...]

    @property
    def subject(self) -> str:
        return shlex.join(self.command)


@dataclass(frozen=True)
class GitPhase(SubjectPhase):
    """A Git operation through the shared repository owner."""

    checkout: Path
    arguments: tuple[str, ...]

    @property
    def subject(self) -> str:
        return shlex.join(("git", "-C", str(self.checkout), *self.arguments))


@dataclass(frozen=True, kw_only=True)
class ProbePhase(SubjectPhase):
    """A declared operation answered by an interpreter worker."""

    probe: type[RuntimeProbe]

    @property
    def subject(self) -> str:
        return self.probe.declared_name


@dataclass(frozen=True, kw_only=True)
class CliProbePhase(ProbePhase):
    """A CLI probe retains the command arguments it measures."""

    arguments: tuple[str, ...]

    @property
    def subject(self) -> str:
        return f"{super().subject} {shlex.join(self.arguments)}"


class PracticePhase(InstallPhase):
    """Select and rehearse the incoming changes on their practice bus."""


class CopyPhase(InstallPhase):
    """Copy and retain the practice inputs selected by their file owners."""


class VerifyPhase(InstallPhase):
    """Read back the carried bus and compare the files the steps changed."""


class ReadBackVerifyPhase(VerifyPhase):
    """Read back the carried bus through its selected runtime."""


class LivePhase(InstallPhase):
    """Stop owners, carry the held bus, publish and restart its captured owners."""


class BridgePhase(InstallPhase):
    """Discover, pause or resume the session bridges around the live swap."""


class ConfirmBridgePhase(BridgePhase):
    """Confirm bridges observe the selected runtime."""


class DiscoverAndPauseBridgePhase(BridgePhase):
    """Discover and pause bridges before carrying the live bus."""


class ReleaseBridgePhase(BridgePhase):
    """Release bridges after the live swap."""


class MaintenancePhase(InstallPhase):
    """Set up, transfer or clear the install's maintenance hold."""


class OutgoingRuntimeMaintenancePhase(MaintenancePhase):
    """Record the outgoing runtime before acquiring maintenance."""


class BeginMaintenancePhase(MaintenancePhase):
    """Acquire the install's maintenance hold after services are stopped."""


class RetargetMaintenancePhase(MaintenancePhase):
    """Transfer the maintenance hold to the staged runtime."""


class AbortMaintenancePhase(MaintenancePhase):
    """Clear maintenance after an install aborts before backup."""


class ClearMaintenancePhase(MaintenancePhase):
    """Clear maintenance after publishing the selected runtime."""


class FileScanPhase(InstallPhase):
    """Inventory the bus files before or after a migration."""


class BeforeMigrationFileScanPhase(FileScanPhase):
    """Inventory bus files before the carry pass."""


class AfterMigrationFileScanPhase(FileScanPhase):
    """Inventory bus files after the carry pass."""


class BackupPhase(InstallPhase):
    """Reserve restore space or save the selected stores and route."""


class StoresAndRouteBackupPhase(BackupPhase):
    """Save the selected stores and the live route."""


class RestoreSpaceBackupPhase(BackupPhase):
    """Reserve enough space to restore the selected stores."""


class BackupRetirePhase(InstallPhase):
    """Remove old rollback backups after acceptance."""


class ServicesPhase(InstallPhase):
    """Stop or start host services."""


class StopServicesPhase(ServicesPhase):
    """Stop host services before carrying or restoring the bus."""


class StartServicesPhase(ServicesPhase):
    """Start host services and prove they serve the selected runtime."""


class ProcessScanPhase(InstallPhase):
    """Read the process table to report applications using older runtimes."""


class PublicationPhase(InstallPhase):
    """Publish the route, runtime or install result through its existing owner."""


class RuntimePublicationPhase(PublicationPhase):
    """Publish the selected runtime's launch paths."""


class StartingReportPublicationPhase(PublicationPhase):
    """Publish the install's starting report."""


class RouteAndRuntimePublicationPhase(PublicationPhase):
    """Publish the selected route and runtime together."""


class FailureReportPublicationPhase(PublicationPhase):
    """Publish the install's failure report."""


class FinishedReportPublicationPhase(PublicationPhase):
    """Publish the install's finished report."""


class WorkerClosePhase(InstallPhase):
    """Close and reap the install's persistent interpreter workers."""


@dataclass(frozen=True)
class RuntimeWorkerClosePhase(SubjectPhase, WorkerClosePhase):
    """Close and reap interpreter workers for one selected runtime."""

    python: Path

    @property
    def subject(self) -> str:
        return str(self.python)


class TreeClosePhase(InstallPhase):
    """Retire the target worktree and the install's scratch directory."""


@dataclass
class PhaseTiming:
    phase: InstallPhase
    seconds: float = 0.0
    """Exclusive wall time: overlapping work is charged once, to the last active phase."""
    elapsed_seconds: float = 0.0
    """The phase's own elapsed run, including its nested or concurrent work."""

    def line(self) -> str:
        return f"{self.seconds:.3f}s {self.phase.label()} ({self.elapsed_seconds:.3f}s elapsed)"

    def launched(self) -> LaunchedPhaseTiming:
        return LaunchedPhaseTiming(self.phase.launched(), self.seconds, self.elapsed_seconds)


@dataclass(frozen=True)
class LaunchedPhase:
    """One launcher phase as every deployed launcher encodes it. The installed launcher
    is always older than the tree it starts, so this shape never changes.
    """

    kind: type[InstallPhase]
    detail: str = ""

    def label(self) -> str:
        return ": ".join(filter(None, (self.kind.__name__, self.detail)))


@dataclass
class LaunchedPhaseTiming:
    phase: LaunchedPhase
    seconds: float
    elapsed_seconds: float

    def timing(self) -> PhaseTiming:
        return PhaseTiming(InheritedPhase(self.phase), self.seconds, self.elapsed_seconds)


@dataclass(frozen=True)
class InheritedPhase(InstallPhase):
    """A phase the launcher timed in the outgoing runtime, labelled as that runtime declared it."""

    outgoing: LaunchedPhase

    def label(self) -> str:
        return self.outgoing.label()


@dataclass
class InstallClock:
    """The single timing record and active execution stack shared by all install workers.

    Each phase boundary advances the same clock. Exclusive seconds partition the
    run, rather than adding a parent's duration to its children's durations. A
    concurrent operation shares that wall interval; its elapsed_seconds still
    records its full duration. There is no per-command or per-copy timer.
    """

    current: ClassVar[ContextVar[InstallClock]] = ContextVar("install_clock")
    phases: list[PhaseTiming] = field(default_factory=list)
    active: list[PhaseTiming] = field(default_factory=list, init=False)
    lock: Lock = field(default_factory=Lock, init=False)
    previous: float = field(default_factory=time.monotonic, init=False)

    @contextmanager
    def selected(self) -> Iterator[InstallClock]:
        token = self.current.set(self)
        try:
            yield self
        finally:
            self.current.reset(token)

    def advance(self) -> float:
        now = time.monotonic()
        if self.active:
            self.active[-1].seconds += now - self.previous
        self.previous = now
        return now

    def include(self, phases: list[PhaseTiming], worker: PhaseTiming) -> None:
        """The serial carry worker measured part of this run already: replace that
        part of its parent's seconds, rather than counting the subprocess twice."""
        with self.lock:
            self.advance()
            worker.seconds -= sum(phase.seconds for phase in phases)
            self.phases.extend(phases)


class InterpreterOperation[Answer, *Arguments](ABC):
    """The operation's return annotation owns its answer on both sides of the process."""

    @abstractmethod
    def run(self, *arguments: *Arguments) -> Answer: ...

    def answer_type(self) -> TypeForm[Answer]:
        return cast(TypeForm[Answer], inspect.signature(self.run, eval_str=True).return_annotation)


class ProbeProgram(ABC):
    """Where an operation's declaration is executable by the selected interpreter."""

    @abstractmethod
    def entry(self) -> tuple[str | Path, ...]: ...

    def import_roots(self) -> tuple[Path, ...]:
        """Directories the interpreter imports from beside its own packages."""
        return ()


class SourceProgram(ProbeProgram):
    """The target's file can run against a predecessor that does not declare it yet."""

    def entry(self) -> tuple[str, str]:
        return RetainedProgram.current().entry()


class InstalledProgram(ProbeProgram):
    """A published runtime owns its declaration after the target tree is retired."""

    def entry(self) -> tuple[str, str]:
        return "-c", f"from {__name__} import serve; serve()"


@dataclass(frozen=True)
class TargetProgram(InstalledProgram):
    """The runtime built from the target tree declares every operation, carry passes
    included; their steps ship in no runtime, so the tree's ``steps`` supply them."""

    steps: Path

    def import_roots(self) -> tuple[Path]:
        return (self.steps,)


@dataclass(frozen=True)
class RetainedProgram(ProbeProgram):
    """Worker declarations that survive retirement of their source checkout."""

    source: str

    @classmethod
    def current(cls) -> RetainedProgram:
        """The snapshot owns the whole installer package; the selected runtime owns the rest of Core."""
        directory = Path(inspect.getfile(RuntimeProbe)).parent
        output = BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for path in sorted(directory.rglob("*.py")):
                archive.write(path, str(path.relative_to(directory.parent.parent)))
        payload = base64.b85encode(output.getvalue()).decode("ascii")
        return cls(
            "import base64\n"
            "from pathlib import Path\n"
            "from tempfile import TemporaryDirectory\n"
            "import agent_comms.installer\n"
            "cache = Path.home() / '.cache'\n"
            "cache.mkdir(parents=True, exist_ok=True)\n"
            "with TemporaryDirectory(prefix='agent-comms-probes-', dir=cache) as retained:\n"
            "    archive = Path(retained) / 'installer.zip'\n"
            f"    archive.write_bytes(base64.b85decode({payload!r}))\n"
            "    agent_comms.installer.__path__ = [str(archive) + '/agent_comms/installer']\n"
            "    from agent_comms.installer.interpreter_operation import serve\n"
            "    serve()\n"
        )

    def entry(self) -> tuple[str, str]:
        return "-c", self.source


@dataclass(frozen=True)
class RuntimeProbe[Answer](
    InterpreterOperation[Answer],
    DeclaredFamily,
    affix="Probe",
    discovery_package="agent_comms.installer.probes",
):
    """A declared operation handled by the install's selected interpreter worker."""

    def install_phase(self) -> InstallPhase:
        return ProbePhase(probe=type(self))


@dataclass(frozen=True)
class ProbeReply:
    """One worker answer, encoded according to its probe, and that request's phases."""

    answer: JsonValue
    phases: list[PhaseTiming]


@dataclass(frozen=True)
class ProbeRequest:
    """One operation and the maintenance hold its caller acts under. A worker outlives holds,
    so the holder travels with each request and never with the worker's launch."""

    probe: RuntimeProbe
    holder: str

    def answer(self) -> ProbeReply:
        """The worker acts as its caller's holder for this request. Its runtime may predate
        ``MaintenanceHolder``, so this sets the variable that owner reads; every request
        sets it, so no earlier request's holder remains."""
        os.environ[HOLDER_NONCE] = self.holder
        clock = InstallClock()
        with clock.selected(), self.probe.install_phase().running(clock):
            answer = FieldCodec.encode(self.probe.run(), self.probe.answer_type())
        return ProbeReply(answer, clock.phases)


def serve() -> None:
    """Answer declared operations from stdin, one encoded answer per request line, until it
    closes. An operation that raises ends the worker: its traceback goes to stderr, and the
    caller reads the exit in place of an answer. The answers own stdout's descriptor; every
    other write, an operation's exit handlers included, lands on stderr."""
    answers = os.fdopen(os.dup(sys.stdout.fileno()), "w")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    for line in sys.stdin:
        print(
            FieldCodec.compact(
                FieldCodec.encode(FieldCodec.decode(ProbeRequest, FieldCodec.parse(line)).answer())
            ),
            file=answers,
            flush=True,
        )
