"""Declared operations sent to an interpreter, with answers separate from diagnostics.

Runtime probes execute this file by path with ``python -P``. The selected runtime
supplies its own imports even when it predates this file.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import field
from threading import Lock
from abc import ABC, abstractmethod
from contextlib import redirect_stdout
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import distributions
from io import StringIO
from pathlib import Path
from typing import Annotated, ClassVar, Self, cast

from pydantic import BaseModel, RootModel
from pydantic.json_schema import DEFAULT_REF_TEMPLATE
from typing_extensions import TypeForm

import agent_comms
from agent_comms.active_route import ActiveRoute, RouteFile, SelectingRoute, active_route_path
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, PathText, WireValue
from agent_comms.maintenance_barrier import HOLDER_NONCE
from agent_comms.service_sender import ServiceSender


@dataclass(frozen=True)
class InstallPhase(DeclaredFamily, affix="Phase"):
    """A declared operation measures its own run through the install's shared clock."""

    detail: str = ""

    def label(self) -> str:
        return f"{type(self).__name__}: {self.detail}" if self.detail else type(self).__name__

    @contextmanager
    def running(self, clock: InstallClock) -> Iterator[PhaseTiming]:
        with clock.lock:
            started = clock.advance()
            measured = PhaseTiming(self)
            clock.phases.append(measured)
            clock.active.append(measured)
        try:
            yield measured
        finally:
            with clock.lock:
                measured.elapsed_seconds = clock.advance() - started
                clock.active[:] = [active for active in clock.active if active is not measured]


class InstallRunPhase(InstallPhase):
    """The whole installer, including teardown and its final publication."""


class InstallerProcessPhase(InstallPhase):
    """Launch the target interpreter, collect its answer and reap it."""


class PreparePhase(InstallPhase):
    """Read the selected target, outgoing runtime and pending operation declarations."""


class BuildPhase(InstallPhase):
    """Resolve and build the target stack and its native package."""


class CommandPhase(InstallPhase):
    """An external command, identified by its actual invocation."""


class GitPhase(InstallPhase):
    """A Git operation through the shared repository owner."""


class ProbePhase(InstallPhase):
    """A declared operation answered by an interpreter worker."""


class PracticePhase(InstallPhase):
    """Select and rehearse the incoming changes on their practice bus."""


class CopyPhase(InstallPhase):
    """Copy and retain the practice inputs selected by their file owners."""


class CarryPhase(InstallPhase):
    """Run a declared carry operation in its target interpreter."""


class VerifyPhase(InstallPhase):
    """Read back the carried bus and compare the files the steps changed."""


class LivePhase(InstallPhase):
    """Stop owners, carry the held bus, publish and restart its captured owners."""


class PublicationPhase(InstallPhase):
    """Publish the route, runtime or install result through its existing owner."""


class WorkerClosePhase(InstallPhase):
    """Close and reap the install's persistent interpreter workers."""


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


@dataclass
class InstallClock:
    """The single timing record and active execution stack shared by all install workers.

    Each phase boundary advances the same clock. Exclusive seconds partition the
    run, rather than adding a parent's duration to its children's durations. A
    concurrent operation shares that wall interval; its elapsed_seconds still
    records its full duration. There is no per-command or per-copy timer.
    """

    phases: list[PhaseTiming] = field(default_factory=list)
    active: list[PhaseTiming] = field(default_factory=list, init=False)
    lock: Lock = field(default_factory=Lock, init=False)
    previous: float = field(default_factory=time.monotonic, init=False)

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

    def encode_answer(self, answer: Answer) -> str:
        return json.dumps(FieldCodec.encode(answer, self.answer_type()))

    def decode_answer(self, answer: str) -> Answer:
        return FieldCodec.decode(self.answer_type(), FieldCodec.parse(answer))


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
    """A source operation that must finish after its owning checkout closes. It runs as the
    interpreter's main module, so the selected runtime's own imports keep its declarations."""

    source: str

    @classmethod
    def current(cls) -> RetainedProgram:
        """The operation family and its worker loop, as this checkout holds them now."""
        path = Path(inspect.getfile(RuntimeProbe))
        return cls(
            f"__file__ = {str(path)!r}\n"
            f"exec(compile({path.read_text()!r}, __file__, 'exec'))\n"
            "serve()\n"
        )

    def entry(self) -> tuple[str, str]:
        return "-c", self.source


@dataclass(frozen=True)
class RuntimeProbe[Answer](
    InterpreterOperation[Answer],
    DeclaredFamily,
    affix="Probe",
    discovery_package="agent_comms.installer",
):
    """A declared operation handled by the install's selected interpreter worker."""

    def install_phase(self) -> InstallPhase:
        return ProbePhase(type(self).__name__)

    def phase_timings(self, answer: Answer) -> list[PhaseTiming]:
        """Nested phases the worker measured while answering this operation."""
        return []


@dataclass(frozen=True)
class CliOutput:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class CliCommandProbe(RuntimeProbe[CliOutput]):
    """One command-line call, answered by the selected runtime's own command line."""

    arguments: tuple[str, ...]

    def run(self) -> CliOutput:
        from agent_comms import cli

        with StringIO() as output, redirect_stdout(output):
            returncode = cli.main(self.arguments)
            return CliOutput(returncode, output.getvalue())


@dataclass(frozen=True)
class ProbeRequest:
    """One operation and the maintenance hold its caller acts under. A worker outlives holds,
    so the holder travels with each request and never with the worker's launch."""

    probe: RuntimeProbe
    holder: str

    def answer(self) -> str:
        """The worker acts as its caller's holder for this request. Its runtime may predate
        ``MaintenanceHolder``, so this sets the variable that owner reads; every request
        sets it, so no earlier request's holder remains."""
        os.environ[HOLDER_NONCE] = self.holder
        return self.probe.encode_answer(self.probe.run())


def serve() -> None:
    """Answer declared operations from stdin, one encoded answer per request line, until it
    closes. An operation that raises ends the worker: its traceback goes to stderr, and the
    caller reads the exit in place of an answer. The answers own stdout's descriptor; every
    other write, an operation's exit handlers included, lands on stderr."""
    answers = os.fdopen(os.dup(sys.stdout.fileno()), "w")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    for line in sys.stdin:
        print(
            FieldCodec.decode(ProbeRequest, FieldCodec.parse(line)).answer(),
            file=answers,
            flush=True,
        )


class ClientImportsProbe(RuntimeProbe[None]):
    def run(self) -> None:
        import_module("toad.app")
        import_module("agent_comms.acp")


class PackageRootProbe(RuntimeProbe[Path]):
    def run(self) -> Annotated[Path, PathText]:
        return Path(agent_comms.__file__).parent


class RouteSelection(DeclaredFamily, affix="Selection", declared_name=None):
    """The outgoing reader's validated choice owns whether this bus may be published."""

    def publish(self, route: ActiveRoute, path: Path, directory: int) -> str:
        return RouteFile.read(path).route_new_bus(route, path, directory)


@dataclass(frozen=True)
class NoRouteSelection(RouteSelection):
    """No route selects an existing bus; the installer may publish this one."""


@dataclass(frozen=True)
class PublishedRouteSelection(RouteSelection):
    root: Annotated[Path, PathText]

    def publish(self, route: ActiveRoute, path: Path, directory: int) -> str:
        if self.root.resolve() != route.root.resolve():
            return f"kept {path}: it selects {self.root}"
        return super().publish(route, path, directory)


class SelectedRouteProbe(RuntimeProbe[RouteSelection]):
    """The outgoing reader validates its own published route before source changes."""

    def run(self) -> RouteSelection:
        route = RouteFile.read(active_route_path())
        if isinstance(route, SelectingRoute):
            return PublishedRouteSelection(route.current.observe_root())
        return NoRouteSelection()


class PrepareNativeProbe(RuntimeProbe[None]):
    def run(self) -> None:
        from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
        from agent_comms.unit_instructions import UnitInstructions

        VerifiedNativePackage.qualify(pinned_native_package())
        UnitInstructions.prepare()


@dataclass(frozen=True)
class RunningOwnersProbe(RuntimeProbe[list[str]]):
    root: Annotated[Path, PathText]

    def run(self) -> list[str]:
        from agent_comms.comms import wire
        from agent_comms.owner_restart import OwnerRestartRequest

        snapshot = wire(self.root).registry.snapshot()
        return [thread.name for thread in OwnerRestartRequest().scope.threads(snapshot)]


@dataclass(frozen=True)
class InstallReportProbe(RuntimeProbe[None], ServiceSender):
    """Publish using the selected runtime's own registry, schema and publisher."""

    sender: ClassVar[str] = "installer"
    root: Annotated[Path, PathText]
    body: str

    def run(self) -> None:
        from agent_comms.channel_targets import CommsTag
        from agent_comms.comms import client_wire

        with client_wire(self.root) as comms:
            self.register(comms.registry)
            comms.messaging.send(self.sender, CommsTag.channel(), self.body)


class ServingProbe(RuntimeProbe[None]):
    """Every running host service serves the root, proven by the runtime that owns it."""

    def run(self) -> None:
        from agent_comms.host_services import HostService

        for member in HostService.members_with(HostService):
            for instance in member.running():
                instance.serves()


class PublicationProbe(RuntimeProbe[None]):
    """The selected interpreter publishes itself: console launchers and harness links move
    to it, then ``current`` does."""

    def run(self) -> None:
        # Imported here: publication -> runtime_resolution -> interpreter_operation is a cycle.
        from agent_comms.installer.publication import RuntimePublication

        RuntimePublication.installed().publish_links()


class DistributionVersionsProbe(RuntimeProbe[dict[str, str]]):
    def run(self) -> dict[str, str]:
        from packaging.utils import canonicalize_name

        return {canonicalize_name(dist.metadata["Name"]): dist.version for dist in distributions()}


@dataclass(frozen=True)
class PackagePin:
    version: str


@dataclass(frozen=True)
class GitPin(PackagePin):
    commit: str


@dataclass(frozen=True)
class ArchivePin(PackagePin):
    url: str


class CommitRecord(BaseModel):
    commit_id: str


class SourceRecord(BaseModel):
    url: str

    def revision(self) -> str:
        raise ValueError("The installed package does not record a Git revision")


class ArchiveRecord(SourceRecord):
    def pin(self, version: str) -> PackagePin:
        return ArchivePin(version, self.url)


class GitRecord(SourceRecord):
    vcs_info: CommitRecord

    def revision(self) -> str:
        return self.vcs_info.commit_id

    def pin(self, version: str) -> PackagePin:
        return GitPin(version, self.vcs_info.commit_id)


class RegistryRecord(RootModel[None]):
    """null is the build record's declared registry-package representation."""

    def revision(self) -> str:
        raise ValueError("A registry package does not record a Git revision")

    def pin(self, version: str) -> PackagePin:
        return PackagePin(version)


class PackageRecord(BaseModel, WireValue):
    version: str
    source: GitRecord | ArchiveRecord | RegistryRecord

    def pin(self) -> PackagePin:
        return self.source.pin(self.version)

    def to_wire(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls.model_validate(data)

    @classmethod
    def schema(
        cls, by_alias: bool = True, ref_template: str = DEFAULT_REF_TEMPLATE
    ) -> dict[str, object]:
        return cls.model_json_schema(by_alias=by_alias, ref_template=ref_template)


@dataclass(frozen=True)
class InstalledPackagesProbe(RuntimeProbe[dict[str, PackageRecord]]):
    core: str

    def run(self) -> dict[str, PackageRecord]:
        from packaging.utils import canonicalize_name

        from agent_comms.package_stamp import STAMP

        if not Path(agent_comms.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError("Core is editable, not a built runtime package")
        actual = STAMP.commit()
        if actual != self.core:
            raise ValueError(
                "The wheel stamp does not name the selected source: "
                f"installed {actual}, requested {self.core}"
            )
        return {
            canonicalize_name(dist.name): PackageRecord.from_wire(
                {
                    "version": dist.version,
                    "source": (
                        json.loads(source)
                        if (source := dist.read_text("direct_url.json"))
                        else None
                    ),
                }
            )
            for dist in distributions()
        }
