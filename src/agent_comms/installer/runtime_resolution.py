"""Compare the installed build's resolved packages with a synced staged lock."""

from __future__ import annotations

import shutil
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlsplit

from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
from pydantic import BaseModel, Field

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.packages import ArchivePin, GitPin, PackagePin, PackageRecord
from agent_comms.installer.probes.packages import DistributionVersionsProbe, InstalledPackagesProbe
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName

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
            parent, separator, filename = entry.filename.partition("/")
            if separator != "/" or filename != "METADATA":
                raise ValueError(f"Malformed wheel metadata path: {entry.filename}")
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

    @staticmethod
    def require_python(specifier: SpecifierSet, environment: dict[str, str]) -> None:
        version = environment["python_full_version"]
        if not specifier.contains(version, prereleases=True):
            raise ValueError(f"The common interpreter {version} does not satisfy {specifier}")


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
        return dict(default_environment())

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
        contracts = tuple(frozenset(str(requirement) for requirement in cls.virtual_requirements(document)
                                    if canonicalize_name(requirement.name) not in local_names
                                    and cls.requirement_active(requirement, environment, ("",)))
                          for document in documents)
        if contracts[0] != contracts[1]:
            raise ValueError("External runtime dependency migration is unsupported by this common environment")

    @staticmethod
    def virtual_requirements(document: dict[str, object]) -> tuple[Requirement, ...]:
        """Only package=false roots have a declarative (non-wheel) runtime contract."""
        match document:
            case {"project": {"dependencies": list() as dependencies, **project},
                  "tool": {"uv": {"package": False}}}:
                python = SpecifierSet(FieldCodec.decode(str, project.get("requires-python", "")))
                ProjectMetadata.require_python(python, dict(default_environment()))
                return tuple(map(Requirement, FieldCodec.decode(tuple[str, ...], dependencies)))
        raise ValueError("A virtual runtime root requires tool.uv.package=false and project dependencies")

    @staticmethod
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
            ProjectMetadata.require_python(project.python, environment)
        selected: dict[str, set[str]] = {}
        pending = [requirement for requirement in cls.virtual_requirements(document)
                   if cls.requirement_active(requirement, environment, ("",))]
        active: set[Requirement] = set()
        while pending:
            requirement = pending.pop()
            # Pending edges were admitted by their declaring package's active extras;
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
