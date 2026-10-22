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
from typing import Annotated, Any

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, PathText


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


@dataclass(frozen=True)
class RuntimeProbe[Answer](InterpreterOperation[Answer], DeclaredFamily, affix="Probe"):
    def command(self, python: Path, result: Path) -> tuple[str | Path, ...]:
        return (python, "-P", Path(__file__), json.dumps(FieldCodec.encode(self)), result)


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


@dataclass(frozen=True)
class InstalledPackagesProbe(RuntimeProbe[dict[str, dict[str, Any]]]):
    core: str

    def run(self) -> dict[str, dict[str, Any]]:
        from importlib.metadata import distributions
        from packaging.utils import canonicalize_name
        import agent_comms
        from agent_comms.package_stamp import STAMP
        if not Path(agent_comms.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError("Core is editable, not a built runtime package")
        if STAMP.commit() != self.core:
            raise ValueError("The wheel stamp does not name the selected source")
        return {canonicalize_name(dist.metadata["Name"]): {
            "version": dist.version,
            "source": json.loads(source) if (source := dist.read_text("direct_url.json")) else None,
        } for dist in distributions()}


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
