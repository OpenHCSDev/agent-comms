"""Declared operations sent to an interpreter, with answers separate from diagnostics.

Runtime probes execute this file by path with ``python -P``. The selected runtime
supplies its own imports even when it predates this file.
"""
from __future__ import annotations

import inspect
import json
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.service_sender import ServiceSender


class InterpreterOperation[Answer](ABC):
    """The operation's return annotation owns its answer on both sides of the process."""

    @abstractmethod
    def run(self, *arguments: object) -> Answer: ...

    def answer_type(self) -> object:
        return inspect.signature(self.run, eval_str=True).return_annotation

    def encode_answer(self, answer: Answer) -> str:
        return json.dumps(FieldCodec.encode(answer, self.answer_type()))

    def decode_answer(self, answer: str) -> Answer:
        return FieldCodec.decode(self.answer_type(), FieldCodec.parse(answer))


class ProbeProgram(ABC):
    """Where an operation's declaration is executable by the selected interpreter."""

    @abstractmethod
    def entry(self, probe: RuntimeProbe) -> tuple[str | Path, ...]: ...


class SourceProgram(ProbeProgram):
    """The target's file can run against a predecessor that does not declare it yet."""

    def entry(self, probe: RuntimeProbe) -> tuple[Path]:
        return (Path(inspect.getfile(type(probe))),)


class InstalledProgram(ProbeProgram):
    """A published runtime owns its declaration after the target tree is retired."""

    def entry(self, probe: RuntimeProbe) -> tuple[str, str]:
        return "-m", type(probe).__module__


@dataclass(frozen=True)
class RetainedProgram(ProbeProgram):
    """A source operation that must finish after its owning checkout closes."""

    path: Path
    source: str

    @classmethod
    def of(cls, declaration: type[RuntimeProbe]) -> RetainedProgram:
        path = Path(inspect.getfile(declaration))
        return cls(path, path.read_text())

    def entry(self, probe: RuntimeProbe) -> tuple[str, str]:
        return "-c", (f"__file__ = {str(self.path)!r}\n"
                      f"exec(compile({self.source!r}, __file__, 'exec'))")


@dataclass(frozen=True)
class RuntimeProbe[Answer](InterpreterOperation[Answer], DeclaredFamily, affix="Probe"):
    def command(self, python: Path, result: Path, *,
                program: ProbeProgram = SourceProgram()) -> tuple[str | Path, ...]:
        return (python, "-P", *program.entry(self),
                json.dumps(FieldCodec.encode(self)), result)


class ClientImportsProbe(RuntimeProbe[None]):
    def run(self) -> None:
        import toad.app  # noqa: F401
        import agent_comms.acp  # noqa: F401


class PinnedLocationProbe(RuntimeProbe[Path]):
    def run(self) -> Annotated[Path, PathText]:
        from agent_comms.native_package import pinned_location
        return pinned_location()


class PackageRootProbe(RuntimeProbe[Path]):
    def run(self) -> Annotated[Path, PathText]:
        import agent_comms
        return Path(agent_comms.__file__).parent


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
        from agent_comms.comms import Comms
        from agent_comms.owner_restart import OwnerRestartRequest
        snapshot = Comms(self.root).registry.snapshot()
        return [thread.name for thread in OwnerRestartRequest().scope.threads(snapshot)]


@dataclass(frozen=True)
class InstallReportProbe(RuntimeProbe[None], ServiceSender):
    """Publish using the selected runtime's own registry, schema and publisher."""

    sender: ClassVar[str] = "installer"
    root: Annotated[Path, PathText]
    body: str

    def run(self) -> None:
        from agent_comms.bus_publication import AgentOrigin
        from agent_comms.channel_targets import CommsTag
        from agent_comms.comms import client_wire
        from agent_comms.messages import InfoKind, Message

        with client_wire(self.root) as comms:
            self.register(comms.registry)
            comms.bus.publisher.publish_ordinary(
                Message(sender=self.sender, target=CommsTag.channel(),
                        body=self.body, type=InfoKind), origin=AgentOrigin())


@dataclass(frozen=True)
class HoldTranscriptsProbe(RuntimeProbe[None]):
    path: Annotated[Path, PathText]
    ready_fd: int

    def run(self) -> None:
        from contextlib import closing
        from agent_comms.locked_store import SQLiteFile
        with closing(SQLiteFile.writer(self.path, isolation_level=None)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with open(self.ready_fd, "wb") as held:
                held.write(b"held")
            sys.stdin.read()
            writer.execute("ROLLBACK")


class DistributionVersionsProbe(RuntimeProbe[dict[str, str]]):
    def run(self) -> dict[str, str]:
        from importlib.metadata import distributions
        from packaging.utils import canonicalize_name
        return {canonicalize_name(dist.metadata["Name"]): dist.version for dist in distributions()}


def main() -> None:
    encoded, result = sys.argv[1:]
    probe = FieldCodec.decode(RuntimeProbe, FieldCodec.parse(encoded))
    Path(result).write_text(probe.encode_answer(probe.run()))


if __name__ == "__main__":
    main()
