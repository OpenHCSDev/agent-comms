"""The installed distribution records shared by probes and runtime resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, RootModel
from pydantic.json_schema import DEFAULT_REF_TEMPLATE

from agent_comms.field_codec import WireValue


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
