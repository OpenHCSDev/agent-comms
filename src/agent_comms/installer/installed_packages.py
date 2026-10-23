"""Installed package records, their source pins and their typed runtime probe."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, RootModel

from agent_comms.installer.interpreter_operation import RuntimeProbe, main


if __name__ == "__main__":
    # The script is an entry point for the canonical declaration, not another member.
    from agent_comms.installer.installed_packages import InstalledPackagesProbe  # noqa: F401

    main()
    raise SystemExit(0)


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


@dataclass(frozen=True)
class InstalledPackagesProbe(RuntimeProbe[dict[str, PackageRecord]]):
    core: str

    def run(self) -> dict[str, PackageRecord]:
        from importlib.metadata import distributions
        from packaging.utils import canonicalize_name
        import agent_comms
        from agent_comms.package_stamp import STAMP

        if not Path(agent_comms.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError("Core is editable, not a built runtime package")
        if STAMP.commit() != self.core:
            raise ValueError("The wheel stamp does not name the selected source")
        return {canonicalize_name(dist.metadata["Name"]): PackageRecord(
            version=dist.version,
            source=json.loads(source) if (source := dist.read_text("direct_url.json")) else None,
        ) for dist in distributions()}

