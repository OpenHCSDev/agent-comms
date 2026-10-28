"""Compare the installed build's resolved packages with a synced staged lock."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlsplit

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.packages import ArchivePin, GitPin, PackagePin, PackageRecord
from agent_comms.installer.probes.packages import DistributionVersionsProbe, InstalledPackagesProbe
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repository_name import RepositoryName

if TYPE_CHECKING:
    from agent_comms.installer.upgrade import Stack


class StackSources:
    """Branch-tracking source declarations shared by installation and source checking."""

    @classmethod
    def sources(cls, stack: Path) -> dict[str, tuple[str, str]]:
        return cls.declared_sources((stack / "pyproject.toml").read_text())

    @staticmethod
    def require_package(checkout: Path, name: str) -> None:
        """A stack key names the actual source project, not a package-to-PR roster."""
        match tomllib.loads((checkout / "pyproject.toml").read_text()):
            case {"project": {"name": str(actual)}} if canonicalize_name(actual) == canonicalize_name(name):
                return
        raise ValueError(f"{checkout} does not declare stack package {name}")

    @staticmethod
    def require_compatible(baseline: str, candidate: str) -> None:
        """Local-source membership may change; external runtime requirements must agree.

        This supported path compares the same external contract, not arbitrary requirement
        migrations. Candidate metadata/settings are copied intact by the private project writer.
        """
        documents = tuple(tomllib.loads(text) for text in (baseline, candidate))
        local_names: set[str] = set()
        for document in documents:
            match document:
                case {"tool": {"uv": {"sources": dict() as sources}}}:
                    for name, source in sources.items():
                        match source:
                            case {"git": str(), "branch": str()} | {"path": str()}:
                                local_names.add(canonicalize_name(FieldCodec.decode(str, name)))
        contracts = []
        for document in documents:
            match document:
                case {"project": {"dependencies": list() as dependencies, **project}}:
                    requirements = tuple(Requirement(text) for text in FieldCodec.decode(tuple[str, ...], dependencies))
                    contracts.append(frozenset(str(requirement) for requirement in requirements
                                              if canonicalize_name(requirement.name) not in local_names))
                    version = ".".join(map(str, sys.version_info[:3]))
                    python = FieldCodec.decode(str, project.get("requires-python", ""))
                    if not SpecifierSet(python).contains(version, prereleases=True):
                        raise ValueError(f"The common interpreter {version} does not satisfy {python}")
                case _:
                    raise ValueError("A compared source requires explicit project runtime dependencies")
        if contracts[0] != contracts[1]:
            raise ValueError("External runtime dependency migration is unsupported by this common environment")

    @staticmethod
    def declared_sources(document: str) -> dict[str, tuple[str, str]]:
        match tomllib.loads(document):
            case {"tool": {"uv": {"sources": dict() as sources}}}:
                found: dict[str, tuple[str, str]] = {}
                for name, source in sources.items():
                    match source:
                        case {"git": str(url), "branch": str(branch)}:
                            GitSource.require_branches(name, (branch,))
                            found[FieldCodec.decode(str, name)] = (url, branch)
                return found
        raise ValueError("The stack declares no [tool.uv.sources] table")


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


class LockSource(BaseModel):
    """One lock package's recorded source; only a branch-tracking Git source is relocked."""

    def tracked(self, name: str) -> tuple[str, ...]:
        return ()


class RegistrySource(LockSource):
    registry: str

    def pin(self, version: str, core: str) -> PackagePin:
        return PackagePin(version)


class GitSource(LockSource):
    """Our sibling packages track main, the ref InstallDue measures them against; a unit
    branch here pins the stack to a head main already merged, and every install relocks it
    unchanged (f39412d88 held Toad on work/unit-toad--P694 after #1239 merged, 2026-10-25).
    A ``?rev=`` source is pinned and never relocked."""

    git: str

    def tracked(self, name: str) -> tuple[str, ...]:
        branches = tuple(parse_qs(urlsplit(self.git).query).get("branch", []))
        self.require_branches(name, branches)
        return (name,) if branches else ()

    @staticmethod
    def require_branches(name: str, branches: tuple[str, ...]) -> None:
        if branches not in ((), ("main",)):
            raise ValueError(f"{name} tracks {branches}, not main")

    def pin(self, version: str, core: str) -> PackagePin:
        commit = urlsplit(self.git).fragment
        if not commit:
            raise ValueError("A resolved Git lock entry must name its commit")
        return GitPin(version, commit)


class EditableSource(LockSource):
    editable: Path

    def pin(self, version: str, core: str) -> PackagePin:
        if self.editable != Path(".."):
            raise ValueError(f"Unrecorded editable source: {self.editable}")
        return GitPin(version, core)


class VirtualSource(LockSource):
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

    @classmethod
    def read(cls, lock: Path) -> ResolvedLock:
        return cls.model_validate(tomllib.loads(lock.read_text()))

    def tracked(self) -> tuple[str, ...]:
        """Every package whose recorded source follows a branch, each named once."""
        return tuple(dict.fromkeys(
            name for entry in self.package for name in entry.source.tracked(entry.name)))

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

    def matches(self, lock: Path, stack: Stack, core: str) -> bool:
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
    def capture(cls, stack: Stack, core: str) -> RuntimeResolution:
        packages = stack.probe(InstalledPackagesProbe(core))
        return cls(core_commit=core, packages=packages)

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

    def matches(self, lock: Path, stack: Stack, core: str) -> bool:
        resolved = ResolvedLock.read(lock)
        print(f"install: real lock decodes {len(resolved.package)} packages", file=sys.stderr)
        # sync --locked has already selected the platform, groups and extras. Read its
        # selected names/versions, then obtain every source identity from the lock itself.
        selected = stack.probe(DistributionVersionsProbe())
        return self.pins() == resolved.pins(selected, core)
