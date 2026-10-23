"""Declared operations sent to an interpreter, with answers separate from diagnostics.

Runtime probes execute this file by path with ``python -P``. The selected runtime
supplies its own imports even when it predates this file.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
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
from agent_comms.active_route import RouteFile, RouteSelection, active_route_path
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, PathText, WireValue
from agent_comms.maintenance_barrier import HOLDER_NONCE
from agent_comms.service_sender import ServiceSender


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


class SelectedRouteProbe(RuntimeProbe[RouteSelection]):
    """The staged runtime reads the published route before source changes."""

    def run(self) -> RouteSelection:
        return RouteFile.read(active_route_path()).selection()


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
