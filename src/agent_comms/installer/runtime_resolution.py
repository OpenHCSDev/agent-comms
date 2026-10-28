"""Compare the installed build's resolved packages with a synced staged lock."""

from __future__ import annotations

import hashlib
import platform
import re
import shutil
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from email import policy
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
from typing import TYPE_CHECKING, Annotated, ClassVar, Self
from urllib.parse import parse_qsl, urlsplit

from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, RootModel

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, GitCommitText, boundary_decoder
from agent_comms.installer.probes.packages import ArchivePin, GitPin, PackagePin, PackageRecord
from agent_comms.installer.probes.packages import DistributionVersionsProbe, InstalledPackagesProbe
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.work_branches import WorkBranches

if TYPE_CHECKING:
    from agent_comms.installer.commands import InstallCommands
    from agent_comms.installer.upgrade import Stack


@dataclass(frozen=True)
class ProjectMetadata:
    """An installable project's backend-produced wheel metadata, not its manifest dialect."""

    name: str
    version: Version
    python: SpecifierSet
    requirements: tuple[Requirement, ...]
    extras: frozenset[str]

    @classmethod
    def from_wheel(cls, wheel: Path) -> ProjectMetadata:
        name, version, _, _ = parse_wheel_filename(wheel.name)
        with ZipFile(wheel) as archive:
            entries = tuple(entry for entry in archive.infolist()
                            if entry.filename.endswith(".dist-info/METADATA"))
            if len(entries) != 1:
                raise ValueError(f"{wheel} requires exactly one dist-info/METADATA")
            entry, = entries
            components = entry.filename.split("/")
            if len(components) != 2:
                raise ValueError(f"Malformed wheel metadata path: {entry.filename}")
            parent, _ = components
            identity, recorded_version = parent.removesuffix(".dist-info").rsplit("-", 1)
            if canonicalize_name(identity) != name or Version(recorded_version) != version:
                raise ValueError(f"Wheel metadata directory differs from {wheel.name}")
            message = BytesParser(policy=policy.default).parsebytes(archive.read(entry))
        if message.defects:
            raise ValueError(f"Malformed wheel metadata: {message.defects}")
        def required(header: str) -> str:
            values = message.get_all(header, [])
            if len(values) != 1 or not str(values[0]).strip():
                raise ValueError(f"Wheel metadata requires exactly one {header}")
            return str(values[0]).strip()
        actual = canonicalize_name(required("Name"), validate=True)
        actual_version = Version(required("Version"))
        Version(required("Metadata-Version"))
        if actual != name or actual_version != version:
            raise ValueError(f"Wheel metadata identity differs from {wheel.name}")
        python = message.get_all("Requires-Python", [])
        if len(python) > 1:
            raise ValueError("Duplicate Requires-Python metadata")
        extra_values = tuple(canonicalize_name(str(value), validate=True)
                             for value in message.get_all("Provides-Extra", []))
        if len(set(extra_values)) != len(extra_values):
            raise ValueError("Duplicate Provides-Extra metadata")
        return cls(actual, actual_version, SpecifierSet(str(python[0]) if python else ""),
                   tuple(Requirement(str(value)) for value in message.get_all("Requires-Dist", [])),
                   frozenset(extra_values))

    @classmethod
    def acquire(cls, checkout: Path, *, uv: Path, python: Path,
                commands: InstallCommands, output: Path) -> ProjectMetadata:
        """Build once through the selected frontend; output never lives in a source cut."""
        output = output.resolve()
        checkout = checkout.resolve()
        if output.is_relative_to(checkout):
            raise ValueError("Wheel metadata output must be outside the captured source")
        before = Repo.git(checkout, "rev-parse", "HEAD").strip()
        if Repo.git(checkout, "status", "--porcelain"):
            raise ValueError(f"Preserve dirty metadata source: {checkout}")
        output.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="metadata-", dir=output) as temporary:
            try:
                commands.run(uv, "build", "--wheel", "--out-dir", temporary,
                             "--python", python, checkout)
                wheels = tuple(Path(temporary).glob("*.whl"))
                if len(wheels) != 1:
                    raise ValueError(f"Backend produced {len(wheels)} wheels for {checkout}, expected one")
                wheel, = wheels
                return cls.from_wheel(wheel)
            finally:
                if Repo.git(checkout, "rev-parse", "HEAD").strip() != before or Repo.git(
                        checkout, "status", "--porcelain"):
                    raise ValueError(f"Backend moved or dirtied captured source: {checkout}")

    def with_all_extras(self, requirement: str) -> str:
        """Enrich only this package's existing root, preserving its complete requirement."""
        selected = Requirement(requirement)
        if canonicalize_name(selected.name) != self.name:
            return requirement
        selected.extras.update(self.extras)
        return str(selected)

    @staticmethod
    def require_python(specifier: SpecifierSet) -> None:
        version = platform.python_version()
        if not specifier.contains(version, prereleases=True):
            raise ValueError(f"The common interpreter {version} does not satisfy {specifier}")


class GitSelection:
    """No requested ref; only the resolved commit selects the package."""

    def branches(self) -> tuple[str, ...]:
        return ()


class GitPackageLocation:
    """The repository root has no native UV subdirectory field."""

    def fields(self) -> dict[str, str]:
        return {}


@dataclass(frozen=True)
class GitQuery(GitSelection, DeclaredFamily, affix="GitQuery"):
    value: str

    def __post_init__(self) -> None:
        if not self.value or chr(0) in self.value:
            raise ValueError("Git query values must be nonempty and representable")
        self.value.encode("utf-8", errors="strict")

    def selectors(self) -> tuple[GitSelection, ...]:
        return ()

    def locations(self) -> tuple[GitPackageLocation, ...]:
        return ()


class GitRef(GitQuery, declared_name=None):
    def selectors(self) -> tuple[GitSelection, ...]:
        return (self,)


class BranchGitQuery(GitRef):
    def branches(self) -> tuple[str, ...]:
        return (self.value,)


class TagGitQuery(GitRef):
    pass


class RevGitQuery(GitRef):
    pass


class SubdirectoryGitQuery(GitQuery, GitPackageLocation):
    def locations(self) -> tuple[GitPackageLocation, ...]:
        return (self,)

    def fields(self) -> dict[str, str]:
        return {self.declared_name: self.value}


@dataclass(frozen=True)
class LockedGitLocation:
    repository: str
    revision: Annotated[str, GitCommitText]
    selector: GitSelection
    package: GitPackageLocation

    @classmethod
    @boundary_decoder
    def decode(cls, value: str) -> LockedGitLocation:
        source = urlsplit(value)
        GitCommitText.decode(source.fragment)
        if len(source.fragment) != 40 or source.fragment != source.fragment.lower():
            raise ValueError("A resolved Git lock entry requires its full lowercase commit")
        if not source.scheme or not source.path:
            raise ValueError("A resolved Git source requires its repository URL")
        if re.search(r"%(?![0-9a-fA-F]{2})", source.query):
            raise ValueError("Malformed Git query escaping")
        parameters: list[GitQuery] = []
        seen: set[str] = set()
        for key, text in parse_qsl(source.query, keep_blank_values=True, strict_parsing=True, errors="strict"):
            if key in seen:
                raise ValueError(f"Duplicate Git query parameter: {key}")
            seen.add(key)
            parameters.append(FieldCodec.decode_document(GitQuery.member_named(key), {"value": text}))
        selectors = tuple(selector for parameter in parameters for selector in parameter.selectors())
        locations = tuple(location for parameter in parameters for location in parameter.locations())
        if len(selectors) > 1:
            raise ValueError("A locked Git source requires one unambiguous ref selector")
        selector, = selectors or (GitSelection(),)
        package, = locations or (GitPackageLocation(),)
        return cls(source._replace(query="", fragment="").geturl(), source.fragment, selector, package)


class UvRecord(BaseModel):
    """Decode consumed TOML fields strictly and preserve every unrelated UV/tool option."""

    model_config = ConfigDict(extra="allow", strict=True, validate_by_alias=True, validate_by_name=False)

    def document(self) -> dict[str, object]:
        # Python mode preserves TOML date/time and arrays, not just JSON-shaped options.
        return self.model_dump(mode="python", by_alias=True, exclude_unset=True)


class UvSourceOptions(UvRecord):
    """Only fields consumed here are modeled; UV retains every other source option."""

    marker: str = ""

    def materialize(self, original: Path, core: Path,
                    projects: dict[RepositoryName, tuple[Path, ProjectMetadata]], name: str) -> UvSourceOptions:
        return self

    def retained_options(self) -> dict[str, object]:
        return self.model_dump(mode="python", by_alias=True, exclude_unset=True)

    def with_options(self, options: dict[str, object], marker: str) -> Self:
        selected = type(self)(**{**options, **self.document()})
        if marker:
            selected.marker = marker
        return selected


class UvGitSource(UvSourceOptions):
    git: str

    def retained_options(self) -> dict[str, object]:
        return self.model_dump(mode="python", by_alias=True, exclude=set(GitQuery.names()), exclude_unset=True)


class UvTrackedSource(UvGitSource):
    branch: str

    def materialize(self, original: Path, core: Path,
                    projects: dict[RepositoryName, tuple[Path, ProjectMetadata]], name: str) -> UvSourceOptions:
        GitSource.require_branches(name, (self.branch,))
        checkout, metadata = projects[Repo.repository_url(self.git)]
        StackSources.require_package(metadata, name)
        options = self.model_dump(mode="python", by_alias=True, exclude_unset=True,
                                  exclude=set(type(self).model_fields) - set(UvSourceOptions.model_fields))
        return UvLocalSource.model_validate({**options, "path": str(checkout), "editable": True})


class UvLocalSource(UvSourceOptions):
    path: str

    def materialize(self, original: Path, core: Path,
                    projects: dict[RepositoryName, tuple[Path, ProjectMetadata]], name: str) -> UvSourceOptions:
        checkout = (original / self.path).resolve()
        if checkout != core or name != CorePackage.distribution:
            raise ValueError(f"Uncaptured local source {name}: {checkout}")
        return self.model_copy(update={"path": str(checkout)})


class UvRevisionSource(UvGitSource):
    rev: str
    subdirectory: str = ""


class UvIndexSource(UvSourceOptions):
    index: str


class UvConditionalSources(RootModel[list[UvSourceOptions]]):
    """Conditional source arrays pass through intact; an exact pin cannot replace them."""

    def materialize(self, original: Path, core: Path,
                    projects: dict[RepositoryName, tuple[Path, ProjectMetadata]], name: str) -> UvConditionalSources:
        return self

    def retained_options(self) -> dict[str, object]:
        raise ValueError("Ambiguous private conditional source declarations")


class UvIndex(UvRecord):
    name: str = ""
    url: str
    explicit: bool = False


class UvProjectSettings(UvRecord):
    package: bool = True
    sources: dict[str, UvRevisionSource | UvTrackedSource | UvLocalSource | UvIndexSource
                  | UvGitSource | UvSourceOptions | UvConditionalSources]
    overrides: list[str] = Field(default_factory=list, alias="override-dependencies")
    constraints: list[str] = Field(default_factory=list, alias="constraint-dependencies")
    indexes: list[UvIndex] = Field(default_factory=list, alias="index")

    def retain(self, name: str, marker: str, source: UvSourceOptions) -> None:
        original = self.sources.get(name, UvSourceOptions())
        self.sources[name] = source.with_options(original.retained_options(), marker)

    def override_git(self, name: str, marker: str) -> None:
        """The native source owns Git location; the root override only selects its name."""
        locked = Requirement(name)
        if marker:
            locked.marker = Marker(marker)
        result: list[str] = []
        found = False
        for text in self.overrides:
            requirement = Requirement(text)
            if canonicalize_name(requirement.name) == name:
                found = True
                if requirement.specifier:
                    self.constraints = [*self.constraints, str(requirement)]
                requirement.specifier = SpecifierSet()
                requirement.url = None
                result.append(str(requirement))
            else:
                result.append(text)
        if not found:
            result.append(str(locked))
        self.overrides = result


class UvProjectRoot(UvRecord):
    dependencies: list[str]


class UvProjectTools(UvRecord):
    uv: UvProjectSettings


class PrivateUvProject(UvRecord):
    """One transient, lossless projection of the actual captured virtual project."""
    project: UvProjectRoot
    tool: UvProjectTools

    @classmethod
    def read(cls, path: Path) -> PrivateUvProject:
        return FieldCodec.decode(cls, tomllib.loads(path.read_text()))

    def enrich(self, metadata: ProjectMetadata) -> None:
        if not any(canonicalize_name(Requirement(text).name) == metadata.name
                   for text in self.project.dependencies):
            raise ValueError("The private checker requires the declared Core runtime root")
        self.project.dependencies = [metadata.with_all_extras(text) for text in self.project.dependencies]
        self.tool.uv.overrides = [metadata.with_all_extras(text) for text in self.tool.uv.overrides]

    def materialize(self, original: Path, core: Path,
                    projects: dict[RepositoryName, tuple[Path, ProjectMetadata]]) -> None:
        for name, source in tuple(self.tool.uv.sources.items()):
            self.tool.uv.sources[name] = source.materialize(original, core, projects, canonicalize_name(name))


class StackSources:
    """Branch-tracking source declarations shared by installation and source checking."""

    @classmethod
    def sources(cls, stack: Path) -> dict[str, tuple[str, str]]:
        return cls.declared_sources((stack / "pyproject.toml").read_text())

    @staticmethod
    def require_package(metadata: ProjectMetadata, name: str) -> None:
        """The actual backend, never a manifest spelling, owns the distribution identity."""
        if metadata.name != canonicalize_name(name):
            raise ValueError(f"Backend builds {metadata.name}, not stack package {name}")

    @staticmethod
    @boundary_decoder
    def frontend() -> Path:
        executable = shutil.which("uv")
        if executable is None:
            raise ValueError("Bootstrap Core's declared uv before acquiring source metadata")
        return Path(executable).absolute()

    @staticmethod
    def marker_environment(python: Path) -> dict[str, str]:
        # Marker evaluation must describe the interpreter actually running both checkers.
        # A different environment cannot borrow this process's platform or Python facts.
        if not LaunchRuntime.from_interpreter(python).runs_this_process:
            raise ValueError("Run metadata preparation in the selected common interpreter")
        return FieldCodec.decode(dict[str, str], default_environment())

    @classmethod
    def require_compatible(cls, baseline: str, candidate: str) -> None:
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
        environment = cls.marker_environment(Path(sys.executable))
        contracts = tuple(frozenset(str(requirement) for requirement in cls.virtual_requirements(document, environment)
                                    if canonicalize_name(requirement.name) not in local_names)
                          for document in documents)
        if contracts[0] != contracts[1]:
            raise ValueError("External runtime dependency migration is unsupported by this common environment")

    @classmethod
    def virtual_requirements(cls, document: dict[str, object], environment: dict[str, str]) -> tuple[Requirement, ...]:
        """Only package=false roots have a declarative (non-wheel) runtime contract."""
        match document:
            case {"project": {"dependencies": list() as dependencies, **project},
                  "tool": {"uv": {"package": False}}}:
                python = SpecifierSet(FieldCodec.decode(str, project.get("requires-python", "")))
                ProjectMetadata.require_python(python)
                return tuple(requirement for requirement in map(
                    Requirement, FieldCodec.decode(tuple[str, ...], dependencies))
                    if cls.requirement_active(requirement, environment, ("",)))
        raise ValueError("A virtual runtime root requires tool.uv.package=false and project dependencies")

    @staticmethod
    @boundary_decoder
    def requirement_active(requirement: Requirement, environment: dict[str, str], extras: Iterable[str]) -> bool:
        return requirement.marker is None or any(
            requirement.marker.evaluate({**environment, "extra": extra}) for extra in extras)

    @classmethod
    def active_requirements(cls, document: dict[str, object], projects: tuple[ProjectMetadata, ...],
                            environment: dict[str, str]) -> tuple[Requirement, ...]:
        """Monotone local-extra closure, retaining original marked/extras-bearing edges."""
        local = {project.name: project for project in projects}
        if len(local) != len(projects):
            raise ValueError("Captured package metadata contains duplicate distribution identities")
        for project in projects:
            ProjectMetadata.require_python(project.python)
        selected: dict[str, set[str]] = {}
        pending = list(cls.virtual_requirements(document, environment))
        active: set[Requirement] = set()
        while pending:
            requirement = pending.pop()
            # Pending edges were selected by their declaring package's active extras;
            # virtual roots have only the base context.
            active.add(requirement)
            name = canonicalize_name(requirement.name)
            if name not in local:
                continue
            project = local[name]
            requested = {"", *(canonicalize_name(extra) for extra in requirement.extras)}
            if not requested.difference({""}).issubset(project.extras):
                raise ValueError(f"{name} does not provide requested extras {requirement.extras}")
            previous = selected.setdefault(name, set())
            added = requested - previous
            if not added:
                continue
            previous.update(added)
            pending.extend(edge for edge in project.requirements if cls.requirement_active(edge, environment, added))
        return tuple(sorted(active, key=str))

    @staticmethod
    def declared_sources(document: str) -> dict[str, tuple[str, str]]:
        match tomllib.loads(document):
            case {"tool": {"uv": {"sources": dict() as sources}}}:
                found: dict[str, tuple[str, str]] = {}
                for name, source in sources.items():
                    match source:
                        case {"git": str(url), "branch": str(branch)}:
                            GitSource.require_branches(name, (branch,))
                            identity = canonicalize_name(FieldCodec.decode(str, name))
                            if identity in found:
                                raise ValueError(f"The stack declares package {identity} twice")
                            found[identity] = (url, branch)
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

    def project_source(self) -> UvSourceOptions:
        raise ValueError("Local/virtual sources have no external retention projection")

    def retain(self, name: str, marker: str, uv: UvProjectSettings) -> None:
        uv.retain(name, marker, self.project_source())


class RegistrySource(LockSource):
    registry: str

    def pin(self, version: str, core: str) -> PackagePin:
        return PackagePin(version)

    def project_source(self) -> UvIndexSource:
        return UvIndexSource(index="locked-" + hashlib.sha256(self.registry.encode()).hexdigest())

    def retain(self, name: str, marker: str, uv: UvProjectSettings) -> None:
        index = UvIndex(name=self.project_source().index, url=self.registry, explicit=True)
        if index not in uv.indexes:
            uv.indexes = [*uv.indexes, index]
        super().retain(name, marker, uv)


class GitSource(LockSource):
    """Our sibling packages track main, the ref InstallDue measures them against; a unit
    branch here pins the stack to a head main already merged, and every install relocks it
    unchanged (f39412d88 held Toad on work/unit-toad--P694 after #1239 merged, 2026-10-25).
    A ``?rev=`` source is pinned and never relocked."""

    model_config = ConfigDict(frozen=True)
    git: str

    @cached_property
    def location(self) -> LockedGitLocation:
        return LockedGitLocation.decode(self.git)

    def tracked(self, name: str) -> tuple[str, ...]:
        branches = self.location.selector.branches()
        self.require_branches(name, branches)
        return (name,) if branches else ()

    @staticmethod
    def require_branches(name: str, branches: tuple[str, ...]) -> None:
        if branches not in ((), (WorkBranches.main,)):
            raise ValueError(f"{name} tracks {branches}, not {WorkBranches.main}")

    def pin(self, version: str, core: str) -> PackagePin:
        return GitPin(version, self.location.revision)

    def project_source(self) -> UvRevisionSource:
        return UvRevisionSource(git=self.location.repository, rev=self.location.revision,
                                **self.location.package.fields())

    def retain(self, name: str, marker: str, uv: UvProjectSettings) -> None:
        super().retain(name, marker, uv)
        uv.override_git(name, marker)


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

    def active(self) -> bool:
        return not self.markers or any(Marker(marker).evaluate() for marker in self.markers)

    def selects(self, name: str, version: str) -> bool:
        return canonicalize_name(self.name) == name and self.active()

    def external(self) -> tuple[VersionedPackage, ...]:
        return ()


class VersionedPackage(LockedPackage):
    version: str
    source: RegistrySource | GitSource

    def selects(self, name: str, version: str) -> bool:
        return super().selects(name, version) and self.version == version

    def external(self) -> tuple[VersionedPackage, ...]:
        return (self,) if self.active() else ()

    def require_retained(self, actual: VersionedPackage) -> None:
        if (canonicalize_name(self.name) != canonicalize_name(actual.name) or self.version != actual.version
                or self.source.project_source() != actual.source.project_source()):
            raise ValueError(f"Private solve changed retained source/version for {self.name}")

    def constrain(self, uv: UvProjectSettings) -> None:
        marker = " or ".join(f"({marker})" for marker in self.markers)
        requirement = Requirement(f"{canonicalize_name(self.name)}=={self.version}")
        if marker:
            requirement.marker = Marker(marker)
        uv.constraints = [*uv.constraints, str(requirement)]
        self.source.retain(canonicalize_name(self.name), marker, uv)


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

    def external(self, excluded: frozenset[str]) -> dict[str, VersionedPackage]:
        """Current-interpreter external evidence; local/virtual entries select themselves out."""
        result: dict[str, VersionedPackage] = {}
        for package in self.package:
            for entry in package.external():
                name = canonicalize_name(entry.name)
                if name in excluded:
                    continue
                if name in result:
                    raise ValueError(f"Lock selects ambiguous external entries for {name}")
                result[name] = entry
        return result

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
