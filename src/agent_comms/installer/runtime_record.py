"""Recorded runtime build.json records and package identities without installer resolution deps."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Self

from pydantic import BaseModel, Field

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.interpreter_operation import RuntimeProbe
from agent_comms.installer.probes.interpreter import InterpreterBuild, RecordedInterpreterBuild
from agent_comms.installer.probes.packages import (
    GitPin,
    PackagePin,
    PackageRecord,
    PreparePackageProbe,
)
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.projects.repository_name import RepositoryName


class RuntimePackage(DeclaredFamily, affix="Package"):
    distribution: ClassVar[str]
    repository: ClassVar[RepositoryName]

    @classmethod
    def commit(cls, resolution: RecordedResolution) -> str:
        return resolution.packages[cls.distribution].source.revision()

    @classmethod
    def pin(cls, resolution: RecordedResolution) -> PackagePin:
        """Package origin owns its pin; an archive is not a repository revision."""
        return resolution.packages[cls.distribution].pin()

    @classmethod
    def preparations(cls) -> tuple[RuntimeProbe[None], ...]:
        """Only packages declaring requirements add target-runtime operations."""
        return ()


class ConsoleScriptPackage:
    """A runtime package whose console scripts the installer publishes."""


class CorePackage(RuntimePackage, ConsoleScriptPackage):
    distribution = "agent-comms"
    repository = RepositoryName("OpenHCSDev", "agent-comms")

    @classmethod
    def commit(cls, resolution: RecordedResolution) -> str:
        return resolution.core_commit

    @classmethod
    def pin(cls, resolution: RecordedResolution) -> PackagePin:
        """Core's built wheel carries its source stamp separately from its URL."""
        return GitPin(resolution.packages[cls.distribution].version, cls.commit(resolution))


class ToadPackage(RuntimePackage, ConsoleScriptPackage):
    distribution = "batrachian-toad"
    repository = RepositoryName("OpenHCSDev", "toad")
    # The module tree the distribution installs, which the client import probe walks.
    import_root = "toad"

    @classmethod
    def preparations(cls) -> tuple[RuntimeProbe[None], ...]:
        return (PreparePackageProbe(cls.distribution),)


class TextualPackage(RuntimePackage):
    distribution = "textual"
    repository = RepositoryName("OpenHCSDev", "textual")


class RecordedResolution(BaseModel):
    """Recorded build.json contents shared by build identity readers and full installer resolution."""

    core_commit: str
    packages: dict[str, PackageRecord]
    interpreter: RecordedInterpreterBuild | InterpreterBuild = Field(
        default_factory=InterpreterBuild
    )

    def reclaimable(self) -> bool:
        """A completed build is reproducible from the commit and lock recorded here, so
        nothing is lost by taking it once no holder names it."""
        return True

    def require_runtime(self, actual: RecordedResolution) -> None:
        """Require recorded interpreter evidence on both sides before comparing inputs."""
        self.interpreter.require_recorded()
        actual.interpreter.require_recorded()
        if self != actual:
            raise UpgradeRefused("Observed runtime differs from its recorded build inputs")

    @classmethod
    def record_path(cls) -> Path:
        return LaunchRuntime.current_pointer() / "build.json"

    @classmethod
    def at(cls, prefix: Path) -> Self:
        return cls.model_validate_json((prefix / cls.record_path().name).read_bytes())

    def wheel_inputs(self) -> tuple[Path, ...]:
        """Only content-qualified archive records request another wheel observation."""
        return tuple(
            wheel
            for package in self.packages.values()
            for wheel in package.source.wheel_inputs()
        )

    def save(self, prefix: Path) -> None:
        path = prefix / self.record_path().name
        path.write_text(self.model_dump_json(indent=2) + "\n")
        path.chmod(0o600)
