"""The installed distribution records shared by probes and runtime resolution."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile
from typing import Self

from packaging.version import Version
from pydantic import BaseModel, Field, JsonValue, RootModel
from pydantic.json_schema import DEFAULT_REF_TEMPLATE

import agent_comms
from agent_comms.build_artifact import BuildArtifact
from agent_comms.nominal.field_codec import Sha256Text, WireValue
from agent_comms.private_path import FileRevision
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe
from agent_comms.package_stamp import STAMP
from agent_comms.runtime_preparation import RuntimePreparation


@dataclass(frozen=True)
class PreparePackageProbe(TargetProbe, RuntimeProbe[None]):
    """Only the built target loads its distribution's preparation implementation."""

    distribution: str

    def run(self) -> None:
        RuntimePreparation.load(self.distribution).prepare()


@dataclass(frozen=True)
class PackagePin:
    version: str


@dataclass(frozen=True)
class GitPin(PackagePin):
    commit: str


@dataclass(frozen=True)
class ArchivePin(PackagePin):
    url: str
    hashes: tuple[tuple[str, str], ...] = ()


class CommitRecord(BaseModel):
    commit_id: str


class WheelSource:
    """Only an archive source can prove installed bytes against a supplied wheel."""

    def verified_wheel(
        self, wheel: Path, distribution: Distribution, runtime: LaunchRuntime
    ) -> ArchiveRecord:
        raise ValueError("A supplied wheel requires an archive installation origin")

    def wheel_inputs(self) -> tuple[Path, ...]:
        return ()


class SourceRecord(BaseModel, WheelSource):
    url: str

    def revision(self) -> str:
        raise ValueError("The installed package does not record a Git revision")


class ArchiveInfo(BaseModel):
    """PEP610's algorithm-to-digest map; an empty map supplies no content proof."""

    hashes: dict[str, str] = Field(default_factory=dict)

    def wheel_inputs(self, url: str) -> tuple[Path, ...]:
        if "sha256" not in self.hashes:
            return ()
        Sha256Text.decode(self.hashes["sha256"])
        source = urlsplit(url)
        # Remote archives retain their PEP610 hashes, not a request to fetch them.
        # Only local wheel inputs can be re-observed without a network operation.
        if source.scheme != "file" or Path(source.path).suffix != ".whl":
            return ()
        if source.netloc not in ("", "localhost"):
            raise ValueError("Runtime wheel input names a foreign file host")
        wheel = Path(unquote(source.path))
        if not wheel.is_absolute():
            raise ValueError("Verified runtime archive is not an absolute wheel input")
        return (wheel,)


class ArchiveRecord(SourceRecord):
    archive_info: ArchiveInfo = Field(default_factory=ArchiveInfo)

    def pin(self, version: str) -> PackagePin:
        return ArchivePin(version, self.url, tuple(sorted(self.archive_info.hashes.items())))

    def wheel_inputs(self) -> tuple[Path, ...]:
        return self.archive_info.wheel_inputs(self.url)

    def verified_wheel(
        self, wheel: Path, distribution: Distribution, runtime: LaunchRuntime
    ) -> ArchiveRecord:
        artifact = BuildArtifact.capture(wheel)
        if self.url != artifact.path.as_uri():
            raise ValueError(f"Installed archive origin differs from supplied wheel: {wheel}")
        with ZipFile(artifact.path) as archive:
            entries = archive.infolist()
            names = tuple(entry.filename for entry in entries)
            records = tuple(name for name in names if name.endswith(".dist-info/RECORD"))
            if len(set(names)) != len(names) or len(records) != 1:
                raise ValueError("A runtime wheel requires unique entries and one RECORD")
            (record,) = records
            for entry in entries:
                relative = PurePosixPath(entry.filename)
                if (relative.is_absolute() or ".." in relative.parts
                        or relative.as_posix() != entry.filename.rstrip("/")):
                    raise ValueError(f"Unsafe wheel member: {entry.filename}")
                if relative.parts[0].endswith(".data"):
                    raise ValueError("Exact runtime inputs require root-layout wheels")
                if entry.is_dir() or entry.filename == record:
                    continue
                installed = distribution.locate_file(entry.filename).resolve(strict=True)
                if not installed.is_relative_to(runtime.prefix):
                    raise ValueError(f"Installed wheel member escapes its runtime: {installed}")
                with archive.open(entry) as source:
                    expected = hashlib.file_digest(source, "sha256").hexdigest()
                if BuildArtifact.capture(installed).sha256 != expected:
                    raise ValueError(f"Installed bytes differ from supplied wheel: {installed}")
        if FileRevision.capture(artifact.path) != artifact.revision:
            raise ValueError(f"Supplied wheel changed during verification: {wheel}")
        known = self.archive_info.hashes
        if "sha256" in known and known["sha256"] != artifact.sha256:
            raise ValueError(f"Archive content differs from its recorded hash: {wheel}")
        return self.model_copy(update={"archive_info": ArchiveInfo(
            hashes={**known, "sha256": artifact.sha256}
        )})


class GitRecord(SourceRecord):
    vcs_info: CommitRecord

    def revision(self) -> str:
        return self.vcs_info.commit_id

    def pin(self, version: str) -> PackagePin:
        return GitPin(version, self.vcs_info.commit_id)


class RegistryRecord(RootModel[None], WheelSource):
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

    def to_wire(self) -> JsonValue:
        return self.model_dump(mode="json")

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls.model_validate(data)

    @classmethod
    def schema(
        cls, by_alias: bool = True, ref_template: str = DEFAULT_REF_TEMPLATE
    ) -> dict[str, JsonValue]:
        return cls.model_json_schema(by_alias=by_alias, ref_template=ref_template)


class DistributionProbe(TargetProbe):
    """The selected interpreter's distribution inventory, rejecting duplicate identities."""

    @staticmethod
    def distributions() -> dict[str, Distribution]:
        from packaging.utils import canonicalize_name

        installed = tuple(distributions())
        selected = {canonicalize_name(dist.name): dist for dist in installed}
        if len(selected) != len(installed):
            raise ValueError("The runtime contains duplicate distribution identities")
        return selected


class DistributionVersionsProbe(DistributionProbe, RuntimeProbe[dict[str, str]]):
    def run(self) -> dict[str, str]:
        return {name: dist.version for name, dist in self.distributions().items()}


@dataclass(frozen=True)
class InstalledPackagesProbe(DistributionProbe, RuntimeProbe[dict[str, PackageRecord]]):
    core: str

    def wheel_inputs(self) -> tuple[Path, ...]:
        """A built donor re-observes its own inputs without changing the request wire."""
        from agent_comms.installer.runtime_resolution import RuntimeResolution

        return RuntimeResolution.at(LaunchRuntime.running().prefix).wheel_inputs()

    def run(self) -> dict[str, PackageRecord]:
        if not Path(agent_comms.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError("Core is editable, not a built runtime package")
        actual = STAMP.commit()
        if actual != self.core:
            raise ValueError(
                "The wheel stamp does not name the selected source: "
                f"installed {actual}, requested {self.core}"
            )
        selected = self.distributions()
        records = {
            name: PackageRecord.from_wire(
                {
                    "version": dist.version,
                    "source": (
                        json.loads(source)
                        if (source := dist.read_text("direct_url.json"))
                        else None
                    ),
                }
            )
            for name, dist in selected.items()
        }
        from agent_comms.installer.runtime_resolution import ProjectMetadata

        for wheel in self.wheel_inputs():
            metadata = ProjectMetadata.from_wheel(wheel)
            record = records[metadata.name]
            if Version(record.version) != metadata.version:
                raise ValueError(f"Installed version differs from supplied wheel: {wheel}")
            records[metadata.name] = record.model_copy(update={
                "source": record.source.verified_wheel(
                    wheel, selected[metadata.name], LaunchRuntime.running()
                )
            })
        return records


@dataclass(frozen=True)
class WheelPackagesProbe(InstalledPackagesProbe):
    """Unrecorded targets supply inputs before their first resolution can be saved."""

    wheels: tuple[Path, ...]

    def wheel_inputs(self) -> tuple[Path, ...]:
        return self.wheels
