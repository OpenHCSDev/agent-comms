"""Compare the installed build's resolved packages with a synced staged lock."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from packaging.markers import Marker
from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field, RootModel, TypeAdapter

from agent_comms.declared_family import DeclaredFamily
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.interpreter_operation import InstalledPackagesProbe, DistributionVersionsProbe
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repository_name import RepositoryName


class RuntimePackage(DeclaredFamily, affix="Package"):
    distribution: ClassVar[str]
    repository: ClassVar[RepositoryName]

    @classmethod
    def commit(cls, resolution: RuntimeResolution) -> str:
        return resolution.packages[cls.distribution].source.revision()


class ConsoleScriptPackage:
    """A runtime package whose console scripts the installer publishes."""


class CorePackage(RuntimePackage, ConsoleScriptPackage):
    distribution = "agent-comms"
    repository = RepositoryName("OpenHCSDev", "agent-comms")

    @classmethod
    def commit(cls, resolution: RuntimeResolution) -> str:
        return resolution.core_commit


class ToadPackage(RuntimePackage, ConsoleScriptPackage):
    distribution = "batrachian-toad"
    repository = RepositoryName("OpenHCSDev", "toad")


class TextualPackage(RuntimePackage):
    distribution = "textual"
    repository = RepositoryName("OpenHCSDev", "textual")


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


class PackageRecord(BaseModel):
    version: str
    source: GitRecord | ArchiveRecord | RegistryRecord

    def pin(self) -> PackagePin:
        return self.source.pin(self.version)


class RegistrySource(BaseModel):
    registry: str

    def pin(self, version: str, core: str) -> PackagePin:
        return PackagePin(version)


class GitSource(BaseModel):
    git: str

    def pin(self, version: str, core: str) -> PackagePin:
        commit = urlsplit(self.git).fragment
        if not commit:
            raise ValueError("A resolved Git lock entry must name its commit")
        return GitPin(version, commit)


class EditableSource(BaseModel):
    editable: Path

    def pin(self, version: str, core: str) -> PackagePin:
        if self.editable != Path(".."):
            raise ValueError(f"Unrecorded editable source: {self.editable}")
        return GitPin(version, core)


class VirtualSource(BaseModel):
    virtual: str

    def pin(self, version: str, core: str) -> PackagePin:
        raise ValueError("A virtual project is not an installed distribution")


class LockedPackage(BaseModel):
    name: str
    markers: list[str] = Field(default_factory=list, alias="resolution-markers")

    def selects(self, name: str, version: str) -> bool:
        return canonicalize_name(self.name) == name and (
            not self.markers or any(Marker(marker).evaluate() for marker in self.markers)
        )


class VersionedPackage(LockedPackage):
    version: str
    source: RegistrySource | GitSource

    def selects(self, name: str, version: str) -> bool:
        return super().selects(name, version) and self.version == version


class LocalPackage(LockedPackage):
    source: EditableSource | VirtualSource


class ResolvedLock(BaseModel):
    package: list[VersionedPackage | LocalPackage]

    def pins(self, selected: dict[str, str], core: str) -> dict[str, PackagePin]:
        result: dict[str, PackagePin] = {}
        for name, version in selected.items():
            candidates = [entry for entry in self.package if entry.selects(name, version)]
            if len(candidates) != 1:
                raise ValueError(f"Lock selects {len(candidates)} entries for {name}=={version}")
            (entry,) = candidates
            result[name] = entry.source.pin(version, core)
        return result


@dataclass(frozen=True)
class UnbuiltRuntime:
    prefix: Path

    def matches(self, lock: Path, python: Path, core: str, commands: InstallCommands) -> bool:
        return False


class RuntimeResolution(BaseModel):
    core_commit: str
    packages: dict[str, PackageRecord]

    @classmethod
    def record_path(cls) -> Path:
        return LaunchRuntime.current_pointer() / "build.json"

    @classmethod
    def at(cls, prefix: Path) -> RuntimeResolution:
        return cls.model_validate_json((prefix / cls.record_path().name).read_bytes())

    @classmethod
    def installed(cls) -> RuntimeResolution | UnbuiltRuntime:
        prefix = LaunchRuntime.installed().prefix
        if not (prefix / cls.record_path().name).is_file():
            return UnbuiltRuntime(prefix)
        return cls.at(prefix)

    @classmethod
    def capture(cls, python: Path, core: str, commands: InstallCommands) -> RuntimeResolution:
        packages = commands.probe(InstalledPackagesProbe(core), python,
                                  env=LaunchRuntime(python).environment(os.environ))
        return cls(core_commit=core,
                   packages=TypeAdapter(dict[str, PackageRecord]).validate_python(packages))

    def save(self, prefix: Path) -> None:
        path = prefix / self.record_path().name
        path.write_text(self.model_dump_json(indent=2) + "\n")
        path.chmod(0o600)

    def pins(self) -> dict[str, PackagePin]:
        result: dict[str, PackagePin] = {
            canonicalize_name(name): record.pin() for name, record in self.packages.items()
        }
        # Wheel URLs locate build output, not the source revisions recorded by this owner.
        for member in RuntimePackage.members_with(RuntimePackage):
            result[member.distribution] = GitPin(
                self.packages[member.distribution].version, member.commit(self)
            )
        return result

    def matches(self, lock: Path, python: Path, core: str, commands: InstallCommands) -> bool:
        resolved = ResolvedLock.model_validate(tomllib.loads(lock.read_text()))
        print(f"install: real lock decodes {len(resolved.package)} packages", file=sys.stderr)
        # sync --locked has already selected the platform, groups and extras. Read its
        # selected names/versions, then obtain every source identity from the lock itself.
        selected = commands.probe(DistributionVersionsProbe(), python,
                                  env=LaunchRuntime(python).environment(os.environ))
        return self.pins() == resolved.pins(selected, core)
