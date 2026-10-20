"""Compare the installed build's resolved packages with a synced staged lock."""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from packaging.markers import Marker
from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field, RootModel, TypeAdapter

from agent_comms.launch_runtime import InstalledBuildManifest, LaunchRuntime


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


class ArchiveRecord(SourceRecord):
    def pin(self, version: str) -> PackagePin:
        return ArchivePin(version, self.url)


class GitRecord(SourceRecord):
    vcs_info: CommitRecord

    def pin(self, version: str) -> PackagePin:
        return GitPin(version, self.vcs_info.commit_id)


class RegistryRecord(RootModel[None]):
    """null is the build record's declared registry-package representation."""

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
    editable: str

    def pin(self, version: str, core: str) -> PackagePin:
        if self.editable != "..":
            raise ValueError(f"Unrecorded editable source: {self.editable}")
        return GitPin(version, core)


class VirtualSource(BaseModel):
    virtual: str

    def pin(self, version: str, core: str) -> PackagePin:
        raise ValueError("A virtual project is not an installed distribution")


class LockedPackage(BaseModel):
    name: str
    version: str
    source: RegistrySource | GitSource | EditableSource | VirtualSource
    markers: list[str] = Field(default_factory=list, alias="resolution-markers")

    def selects(self, name: str, version: str) -> bool:
        return (
            canonicalize_name(self.name) == name
            and self.version == version
            and (not self.markers or any(Marker(marker).evaluate() for marker in self.markers))
        )


class ResolvedLock(BaseModel):
    package: list[LockedPackage]

    def pins(self, selected: dict[str, str], core: str) -> dict[str, PackagePin]:
        result: dict[str, PackagePin] = {}
        for name, version in selected.items():
            candidates = [entry for entry in self.package if entry.selects(name, version)]
            if len(candidates) != 1:
                raise ValueError(f"Lock selects {len(candidates)} entries for {name}=={version}")
            (entry,) = candidates
            result[name] = entry.source.pin(version, core)
        return result


class RuntimeResolution(InstalledBuildManifest):
    packages: dict[str, PackageRecord]

    @classmethod
    def record_path(cls) -> Path:
        return LaunchRuntime.current_pointer() / "build.json"

    @classmethod
    def installed(cls) -> RuntimeResolution:
        return cls.model_validate_json(cls.record_path().read_bytes())

    def pins(self) -> dict[str, PackagePin]:
        result: dict[str, PackagePin] = {
            canonicalize_name(name): record.pin() for name, record in self.packages.items()
        }
        # Wheel URLs locate build output, not the source revisions recorded by this owner.
        for name, commit in (
            ("agent-comms", self.core_commit),
            ("batrachian-toad", self.toad_commit),
        ):
            result[name] = GitPin(self.packages[name].version, commit)
        return result

    def matches(self, lock: Path, python: Path, core: str) -> bool:
        resolved = ResolvedLock.model_validate(tomllib.loads(lock.read_text()))
        # sync --locked has already selected the platform, groups and extras. Read its
        # selected names/versions, then obtain every source identity from the lock itself.
        selected = TypeAdapter(dict[str, str]).validate_json(
            subprocess.check_output(
                [
                    python,
                    "-c",
                    "import importlib.metadata as m,json; "
                    "from packaging.utils import canonicalize_name as n; "
                    'print(json.dumps({n(d.metadata["Name"]):d.version for d in m.distributions()}))',
                ]
            )
        )
        return self.pins() == resolved.pins(selected, core)
