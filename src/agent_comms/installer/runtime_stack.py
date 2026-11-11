"""A built runtime and the source checkout containing its install tools.

Installer-internal: the install's bootstrap builds the target's runtime under the outgoing
interpreter, so nothing here may import Core's bus composition (agent_comms.comms).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import json
from abc import (
    ABC,
    abstractmethod,
)
from dataclasses import dataclass, field, fields
from pathlib import Path
from secrets import token_hex

from packaging.utils import canonicalize_name

from agent_comms.execution.child_process import ResidentManager
from agent_comms.execution.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.execution.test_lease import LeaseStart, TestLease
from agent_comms.installer.build_artifact import BuildArtifact
from agent_comms.installer.building_runtime import BuildingRuntime
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.declared_uv import DeclaredUv
from agent_comms.installer.interpreter_operation import (
    BuildPhase,
    InstalledProgram,
    ProbeProgram,
    RuntimeProbe,
    TargetProgram,
)
from agent_comms.installer.native_build_inputs import NativeBuildInputs
from agent_comms.installer.native_package import NativePackageStore, pinned_package_for
from agent_comms.installer.probes.imports import ClientImportsProbe
from agent_comms.installer.probes.native import PrepareNativeProbe, PrepareProducerDigestsProbe
from agent_comms.installer.probes.resolution import RuntimeResolutionProbe
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.runtime_home import RuntimeHome
from agent_comms.installer.runtime_resolution import (
    ConsoleScriptPackage,
    ProjectMetadata,
    ResolvedLock,
    RuntimePackage,
    RuntimeResolution,
    ToadPackage,
)
from agent_comms.installer.target_tree import STEPS
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.projects.storage_rules import StorageRule

HOME = Path.home()


@dataclass(frozen=True)
class Extendable:
    """A record a subclass extends with facts of its own; the shared ones carry over."""

    def extended[Record](self, kind: type[Record], **facts: object) -> Record:
        return kind(**({each.name: getattr(self, each.name) for each in fields(self)} | facts))


@dataclass(frozen=True)
class RuntimeStack(Extendable):
    """One built installation and the source checkout containing its install tools."""

    checkout: Path
    commands: InstallCommands
    home: Path = HOME
    runtime: LaunchRuntime = field(default_factory=LaunchRuntime.installed)
    program: ProbeProgram = InstalledProgram()
    """How this runtime's worker reaches the operation family: the outgoing runtime answers
    from its own installed installer; a runtime built from the target declares it too,
    with the tree's carry steps beside it."""

    @property
    def python(self) -> Path:
        return self.runtime.interpreter

    @property
    def lock(self) -> Path:
        return self.checkout / "stack/uv.lock"

    @property
    def uv(self) -> Path:
        """The uv the installer runs, kept in the installer's own state."""
        return RuntimeHome.uv_path(self.home)

    def require_uv(self) -> None:
        """The installer runs only the uv its checkout declares; a different one installs the
        declared uv in its own place before any lock is written."""
        if not os.access(self.uv, os.X_OK):
            raise UpgradeRefused(f"No uv executable at {self.uv}; the installer runs only that uv")
        declared = DeclaredUv.of(self.checkout / "pyproject.toml", self.uv)
        if declared.accepts(self.commands.run(self.uv, "--version")):
            return
        with tempfile.TemporaryDirectory(dir=self.uv.parent, prefix=".uv-") as staging:
            self.commands.run(self.uv, "pip", "install", "--quiet", "--target", staging,
                              "--python", self.python, str(declared.requirement))
            fetched = Path(staging) / "bin/uv"
            reported = self.commands.run(fetched, "--version")
            if not declared.accepts(reported):
                raise UpgradeRefused(f"Fetched {reported.strip()}; {declared.requirement} declared")
            os.replace(fetched, self.uv)

    def sync_test_environment(self, checkout: Checkout) -> None:
        """The live checkout's test interpreter follows the stack this install just resolved.

        On 2026-11-10 pyproject declared immutables without a relock: every order's
        interpreter failed at import, and the rule forbade the one sync that would have
        fixed it. Syncing the checkout's own lock fixed that and bought a quieter version of
        the same defect, because `resolve` refreshes the stack and nothing refreshes that
        one: the test interpreter spent days on a NominalRefactorAdvisor whose own gate.py
        and json_reports.py disagreed, while the stack tracked that package's main. Two
        resolutions of our own packages is one too many, so the test interpreter reads the
        resolution the runtime is built from and the other is consulted by nobody.
        """
        self.commands.run(
            self.uv, "sync", "--project", "stack", "--locked", "--all-extras",
            cwd=checkout.path,
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(checkout.test_environment)})

    def resolve(self) -> None:
        """Refresh every package the lock records as tracking a branch, transitive ones
        included, while retaining the committed release and revision pins."""
        self.commands.run(self.uv, "lock", "--project", "stack",
                          *ResolvedLock.read(self.lock).refresh_arguments(), cwd=self.checkout)

    @staticmethod
    def build(checkout: Checkout, commands: InstallCommands, home: Path = HOME) -> TargetRuntime:
        """Select and qualify the whole target runtime before its installer starts."""
        return RuntimeBuild.select(checkout, commands, home).build()

    def sync(self) -> None:
        """Install the lock's packages into this runtime's prefix; a failed sync leaves none.

        A sync that succeeds claims the prefix for this process, because completion happens
        in the image this one execs into and nothing else spans the two.
        """
        prefix = self.runtime.prefix
        prefix.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Claimed before the directory exists, because uv owns what is in it from the
        # moment it starts and a kill during the sync strands exactly as much as a kill
        # after it. The claim covers the construction from its first durable byte.
        BuildingRuntime.claim(prefix)
        prefix.mkdir(mode=0o700, exist_ok=True)
        try:
            self.commands.run(self.uv, "sync", "--project", "stack", "--locked",
                              "--no-editable", cwd=self.checkout,
                              env={**self.environment(), "UV_PROJECT_ENVIRONMENT": str(prefix)})
        except BaseException:
            BuildingRuntime.discard(prefix)
            raise

    def complete(self) -> None:
        """Finish inside this runtime, or re-observe its recorded packages and interpreter.

        Production-cache selection checks the source lock in build/prepare; completion
        also verifies exact explicit-wheel inputs without claiming their Git ancestry.
        """
        with BuildPhase().running(self.commands.clock):
            core = self.commands.git(self.checkout, "rev-parse", "HEAD").strip()
            RuntimeResolution.recorded(self.runtime.prefix).complete(self, core)

    def test(self, *pytest_arguments: str, start: LeaseStart = TestLease.default_start) -> None:
        """Run checkout tests against this runtime's installed packages, never its source."""
        self.commands.run(self.uv, "pip", "install", "--python", self.python,
                          "-r", "pyproject.toml", "--extra", "dev", cwd=self.checkout,
                          env=self.environment())
        core = self.commands.git(self.checkout, "rev-parse", "HEAD").strip()
        scratch = StorageRule().scratch_base
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix=f"tests-{core[:12]}-") as temporary:
            print(self.commands.run(
                self.python, "-m", "pytest", "-q", "-s",
                "-o", "pythonpath=. tools", "-o", "faulthandler_timeout=60",
                "--basetemp", Path(temporary) / "pytest", "-p", "no:cacheprovider",
                *start.arguments(), *pytest_arguments, cwd=self.checkout, env=self.environment(),
                pass_fds=start.descriptors()), end="")

    def probe[Answer](self, probe: RuntimeProbe[Answer]) -> Answer:
        """Every call into this runtime during an install goes to its one worker, which
        imports the runtime once."""
        return self.commands.probe(probe, self.python, cwd=self.home,
                                   env=self.environment(*self.program.import_roots()),
                                   program=self.program)

    def observe_resolution(self, core: str) -> RuntimeResolution:
        return self.probe(RuntimeResolutionProbe(core, self.wheel_inputs()))

    def imports(self) -> None:
        self.probe(ClientImportsProbe())

    def wheel_inputs(self) -> tuple[Path, ...]:
        """Re-observe retained inputs from the record; unbuilt lock targets have none."""
        return RuntimeResolution.recorded(self.runtime.prefix).wheel_inputs()

    def stage_native(self) -> tuple[Path, Path]:
        """Prepare a missing artifact once; the checkout holds only its store link."""
        manifest = self.checkout / "stack/pi-native.sha256"
        try:
            NativeBuildInputs(self.checkout).require_pinned(manifest)
        except ValueError as error:
            raise UpgradeRefused(str(error)) from None
        store = NativePackageStore.at_home(self.home)
        native = store.package_for(manifest)
        if not native.is_dir():
            self.commands.run(self.checkout / "stack/native/prepare-pi-native", "--python",
                              self.python, cwd=self.checkout,
                              env=self.environment(self.checkout / "src"))
        store.link(manifest, manifest.parent)
        return native, pinned_package_for(manifest).relative_to(manifest.parent)

    def install_native(self) -> None:
        """Interpreters bind the store artifact; disposable trees contain only links."""
        self.stage_native()
        NativePackageStore.at_home(self.home).link(
            self.checkout / "stack/pi-native.sha256", self.runtime.package_root / "_native")
        self.prepare_runtime()

    def prepare_runtime(self) -> None:
        self.probe(PrepareProducerDigestsProbe())
        self.probe(PrepareNativeProbe())
        for package in RuntimePackage.members_with(RuntimePackage):
            for preparation in package.preparations():
                self.probe(preparation)

    def environment(self, *paths: Path) -> dict:
        """Use the artifact's packages; ``paths`` are searched first, in order (the target
        tree's carry steps, or its source for a native build)."""
        environment = {**self.runtime.environment(os.environ), "HOME": str(self.home)}
        environment = ResidentManager.current(environment).handoff_environment(environment)
        configuration = RestartEnvironment.inherit(environment)
        for path in reversed(paths):
            configuration = configuration.with_import_root(path)
        return configuration.apply_runtime(environment)


@dataclass(frozen=True)
class TargetRuntime(RuntimeStack):
    """A runtime built from the target tree: its worker declares the tree's carry steps."""

    program: TargetProgram = field(kw_only=True)


@dataclass(frozen=True, kw_only=True)
class WheelRuntime(RuntimeStack):
    """A fresh functional target: explicit Python, retained dependencies, exact wheels.

    This never enters the production Core/lock reuse cache. Completion, native
    preparation, package capture, imports and worker retirement stay on RuntimeStack.
    Local-wheel origins remain archive origins, not invented repository revisions.
    """

    interpreter: LaunchRuntime
    source_runtime: LaunchRuntime
    wheels: tuple[Path, ...]

    @property
    def input_directory(self) -> Path:
        return self.runtime.prefix / "inputs"

    def wheel_inputs(self) -> tuple[Path, ...]:
        """The new runtime retains its own exact inputs; borrowed originals stay read-only."""
        return tuple(self.input_directory / wheel.name for wheel in self.wheels)

    def sync(self) -> None:
        named_prefix = self.runtime.interpreter.parent.parent
        if named_prefix.exists() or named_prefix.is_symlink():
            raise UpgradeRefused(f"Preserve existing candidate: {named_prefix}")
        prefix = self.runtime.prefix
        self.interpreter.require_available()
        self.source_runtime.require_available()
        metadata = tuple(ProjectMetadata.from_wheel(wheel) for wheel in self.wheels)
        selected = {project.name for project in metadata}
        registered = {member.distribution for member in RuntimePackage.members_with(RuntimePackage)}
        required = {member.distribution for member in RuntimePackage.members_with(ConsoleScriptPackage)}
        if len(selected) != len(metadata) or not required <= selected <= registered:
            raise UpgradeRefused("Provide one exact wheel for each console-script package; "
                                 "only declared runtime packages may be replaced")
        declared = DeclaredUv.of(self.checkout / "pyproject.toml", self.uv)
        if not declared.accepts(self.commands.run(self.uv, "--version")):
            raise UpgradeRefused("Candidate preparation requires Core's existing declared uv")
        source = RuntimeResolution.at(self.source_runtime.prefix)
        donor = RuntimeStack(self.checkout, self.commands, self.home, runtime=self.source_runtime)
        source.require_runtime(donor.observe_resolution(source.core_commit))
        artifacts = tuple(BuildArtifact.capture(wheel) for wheel in self.wheels)
        self.commands.run(self.uv, "--offline", "venv", "--python",
                          self.interpreter.interpreter, prefix)
        if self.runtime.interpreter.resolve() != self.interpreter.interpreter.resolve():
            raise UpgradeRefused("Prepared target did not select the requested interpreter")
        self.input_directory.mkdir(mode=0o700)
        for artifact, destination in zip(artifacts, self.wheel_inputs(), strict=True):
            shutil.copyfile(artifact.path, destination)
            destination.chmod(0o400)
            if BuildArtifact.capture(destination).sha256 != artifact.sha256:
                raise UpgradeRefused(f"Retained wheel differs from its input: {artifact.path}")
        shutil.copytree(self.source_runtime.purelib, self.runtime.purelib,
                        dirs_exist_ok=True, symlinks=True)
        # Prefix data is copied intact; package preparation validates its native assets.
        shutil.copytree(self.source_runtime.prefix / "share", prefix / "share", symlinks=True)
        self.commands.run(self.uv, "--offline", "pip", "install", "--no-deps", "--reinstall",
                          "--python", self.python, *self.wheel_inputs(), env=self.environment())
        self.complete_dependencies(metadata, source)
        self.commands.run(self.uv, "--offline", "pip", "check", "--python", self.python,
                          env=self.environment())

    def complete_dependencies(
        self, metadata: tuple[ProjectMetadata, ...], source: RuntimeResolution,
    ) -> None:
        """Exact functional wheel inputs retain their donor dependency set."""

    def prepare(self) -> RuntimeResolution:
        """Prepare once; a retained path is never silently rebound or reused."""
        with BuildPhase().running(self.commands.clock):
            self.sync()
            self.complete()
            return RuntimeResolution.at(self.runtime.prefix)


@dataclass(frozen=True)
class HistoricalRuntime(WheelRuntime, TargetRuntime):
    """Retain working packages and resolve requirements added by the historical source."""

    def complete_dependencies(
        self, metadata: tuple[ProjectMetadata, ...], source: RuntimeResolution,
    ) -> None:
        selected = {project.name for project in metadata}
        constraints = self.input_directory / "recorded-constraints.txt"
        constraints.write_text("".join(
            f"{name}=={package.version}\n"
            for name, package in source.packages.items() if name not in selected
        ))
        additions = self.input_directory / "added-requirements.txt"
        additions.write_text("".join(
            f"{requirement}\n"
            for project in metadata for requirement in project.requirements
            if canonicalize_name(requirement.name) not in source.packages
        ))
        self.commands.run(self.uv, "pip", "install", "--python", self.python,
                          "--constraint", constraints, "--requirements", additions,
                          env=self.environment())


@dataclass(frozen=True)
class RuntimeBuild(ABC):
    """Choose packages once, check the built runtime, then hand it to the target installer."""

    checkout: Checkout
    commands: InstallCommands
    home: Path = HOME

    @property
    def runtimes(self) -> Path:
        """The build directory shared by current and historical targets."""
        return self.home / ".local/share/agent-comms/runtimes"

    @staticmethod
    def select(checkout: Checkout, commands: InstallCommands, home: Path = HOME) -> RuntimeBuild:
        target = commands.git(checkout.path, "rev-parse", "HEAD").strip()
        main = commands.git(checkout.path, "rev-parse", "origin/main").strip()
        if target != main and Repo.descends(checkout.path, target, main):
            return HistoricalRuntimeBuild(checkout, commands, home)
        return MainRuntimeBuild(checkout, commands, home)

    def build(self) -> TargetRuntime:
        with BuildPhase().running(self.commands.clock):
            source = RuntimeStack(self.checkout.path, self.commands, self.home,
                                  runtime=LaunchRuntime.running())
            source.require_uv()
            core = self.commands.git(self.checkout.path, "rev-parse", "HEAD").strip()
            built = self.stage(source, core)
            built.complete()
            return built

    @abstractmethod
    def stage(self, source: RuntimeStack, core: str) -> TargetRuntime:
        """Each selection keeps the shared preparation and qualification path."""
        raise NotImplementedError


class MainRuntimeBuild(RuntimeBuild):
    """Current source follows main once; the resolved lock owns the complete build."""

    def stage(self, source: RuntimeStack, core: str) -> TargetRuntime:
        source.resolve()
        resolution = hashlib.sha256(source.lock.read_bytes()).hexdigest()[:16]
        prefix = self.runtimes / f"core-{core[:9]}-lock-{resolution}"
        built = source.extended(
            TargetRuntime, runtime=LaunchRuntime.at_prefix(prefix),
            program=TargetProgram(self.checkout.path / STEPS.parent))
        RuntimeResolution.recorded(prefix).prepare(built, core)
        return built


class HistoricalRuntimeBuild(RuntimeBuild):
    """Use the installed dependencies, not an old lock or today's unrelated branch heads."""

    def stage(self, source: RuntimeStack, core: str) -> HistoricalRuntime:
        donor = LaunchRuntime.installed()
        recorded = RuntimeResolution.at(donor.prefix)
        if not Repo.descends(self.checkout.path, recorded.core_commit, core):
            raise UpgradeRefused(
                "Historical recovery requires an installed ancestor of the target Core")
        runtimes = self.runtimes
        runtimes.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Explicit wheel builds are never rebound or borrowed from the Core/lock cache.
        prefix = runtimes / f"core-{core[:9]}-recorded-{token_hex(8)}"
        with tempfile.TemporaryDirectory(dir=runtimes, prefix="wheel-") as temporary:
            output = Path(temporary)
            self.commands.run(source.uv, "build", "--wheel", "--out-dir", output,
                              "--python", source.python, self.checkout.path)
            wheels = tuple(output.glob("*.whl"))
            if len(wheels) != 1:
                raise UpgradeRefused("Historical Core must produce exactly one wheel")
            (core_wheel,) = wheels
            client_wheel = self.client_wheel(source, recorded, core, core_wheel, output)
            wheels = (core_wheel, client_wheel)
            built = source.extended(
                HistoricalRuntime, runtime=LaunchRuntime.at_prefix(prefix),
                program=TargetProgram(self.checkout.path / STEPS.parent),
                interpreter=donor, source_runtime=donor, wheels=wheels)
            built.sync()
        return built

    def client_wheel(self, source: RuntimeStack, recorded: RuntimeResolution,
                     core: str, core_wheel: Path, output: Path) -> Path:
        """Rebuild the recorded client with only Git-proven provider import moves."""
        commit = ToadPackage.commit(recorded)
        client = output / ToadPackage.import_root
        repository = Repo.acquire_github(ToadPackage.repository, client)
        repository.acquire(commit)
        self.commands.git(client, "checkout", "--detach", commit)
        self.repair_client(source, recorded.core_commit, core, core_wheel, client, output)
        self.commands.run(source.uv, "build", "--wheel", "--out-dir", output,
                          "--python", source.python, client)
        built = tuple(path for path in output.glob("*.whl")
                      if ProjectMetadata.from_wheel(path).name == ToadPackage.distribution)
        if len(built) != 1:
            raise UpgradeRefused("Historical client must produce exactly one wheel")
        return built[0]


    def repair_client(self, source: RuntimeStack, previous: str, target: str,
                      core_wheel: Path, client: Path, output: Path) -> None:
        """Run NRA from Core's declared dependency, without importing either application."""
        requirements = tuple(requirement for requirement in ProjectMetadata.from_wheel(core_wheel).requirements
                             if requirement.name == "nominal-refactor-advisor")
        if len(requirements) != 1:
            raise UpgradeRefused("Core must declare exactly one NRA dependency")
        (requirement,) = requirements
        plan = output / "client-imports.plan.json"
        plan.write_text(json.dumps({"recipes": [{
            "recipe_id": "historical-client-provider-imports",
            "reason": "Replay the provider's recorded module moves onto the recorded client; preserve its code and dependency set.",
            "operations": [{
                "operation": "replay_git_module_imports",
                "provider_repository": str(self.checkout.path),
                "source_revision": previous,
                "target_revision": target,
                "caller_root": str(client),
            }],
        }]}))
        self.commands.run(source.uv, "tool", "run", "--python", source.python,
                          "--refresh-package", requirement.name,
                          "--from", str(requirement),
                          "nominal-refactor-advisor", client, "--codemod-plan", plan,
                          "--codemod-apply", "--json")
