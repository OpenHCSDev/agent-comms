"""A built runtime and the source checkout containing its install tools.

Installer-internal: the install's bootstrap builds the target's runtime under the outgoing
interpreter, so nothing here may import Core's bus composition (agent_comms.comms).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path

from agent_comms.installer.carry import STEPS
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.declared_uv import DeclaredUv
from agent_comms.installer.interpreter_operation import (
    BuildPhase,
    InstalledProgram,
    ProbeProgram,
    RuntimeProbe,
    TargetProgram,
)
from agent_comms.installer.practice import RuntimeHome
from agent_comms.installer.probes.imports import ClientImportsProbe
from agent_comms.installer.probes.native import PrepareNativeProbe, PrepareProducerDigestsProbe
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.runtime_resolution import ResolvedLock, RuntimeResolution
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.native_build_inputs import NativeBuildInputs
from agent_comms.native_package import NativePackageStore, pinned_package_for
from agent_comms.repo import Checkout
from agent_comms.storage_rules import StorageRule

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

    def resolve(self) -> None:
        """Refresh every package the lock records as tracking a branch, transitive ones
        included, while retaining the committed release and revision pins."""
        upgrades = tuple(argument for name in ResolvedLock.read(self.lock).tracked()
                         for argument in ("--upgrade-package", name))
        self.commands.run(self.uv, "lock", "--project", "stack", *upgrades,
                          cwd=self.checkout)

    @staticmethod
    def build(checkout: Checkout, commands: InstallCommands, home: Path = HOME) -> TargetRuntime:
        """Resolve current mains once, then sync or reuse their immutable installation.

        Only uv runs here: a probe encodes the operation family, which only the built
        runtime's own packages can import, so ``complete`` finishes the runtime from inside it.
        """
        with BuildPhase().running(commands.clock):
            source = RuntimeStack(checkout.path, commands, home, runtime=LaunchRuntime.running())
            source.require_uv()
            source.resolve()
            core = commands.git(checkout.path, "rev-parse", "HEAD").strip()
            resolution = hashlib.sha256(source.lock.read_bytes()).hexdigest()[:16]
            runtimes = home / ".local/share/agent-comms/runtimes"
            prefix = runtimes / f"core-{core[:9]}-lock-{resolution}"
            built = source.extended(
                TargetRuntime, runtime=LaunchRuntime.at_prefix(prefix),
                program=TargetProgram(checkout.path / STEPS.parent))
            RuntimeResolution.recorded(prefix).prepare(built)
            return built

    def sync(self) -> None:
        """Install the lock's packages into this runtime's prefix; a failed sync leaves none."""
        prefix = self.runtime.prefix
        prefix.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        prefix.mkdir(mode=0o700, exist_ok=True)
        try:
            self.commands.run(self.uv, "sync", "--project", "stack", "--locked",
                              "--no-editable", cwd=self.checkout,
                              env={**self.environment(), "UV_PROJECT_ENVIRONMENT": str(prefix)})
        except BaseException:
            shutil.rmtree(prefix)
            raise

    def complete(self) -> None:
        """Finish the synced runtime from inside it: its native package, recorded resolution
        and import check, or the check that a recorded one still matches its inputs."""
        with BuildPhase().running(self.commands.clock):
            core = self.commands.git(self.checkout, "rev-parse", "HEAD").strip()
            RuntimeResolution.recorded(self.runtime.prefix).complete(self, core)

    def test(self, *pytest_arguments: str) -> None:
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
                *pytest_arguments, cwd=self.checkout, env=self.environment()), end="")

    def probe[Answer](self, probe: RuntimeProbe[Answer]) -> Answer:
        """Every call into this runtime during an install goes to its one worker, which
        imports the runtime once."""
        return self.commands.probe(probe, self.python, cwd=self.home,
                                   env=self.environment(*self.program.import_roots()),
                                   program=self.program)

    def imports(self) -> None:
        self.probe(ClientImportsProbe())

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

    def environment(self, *paths: Path) -> dict:
        """Use the artifact's packages; ``paths`` are searched first, in order (the target
        tree's carry steps, or its source for a native build)."""
        environment = {**self.runtime.environment(os.environ), "HOME": str(self.home)}
        configuration = RestartEnvironment.inherit(environment)
        for path in reversed(paths):
            configuration = configuration.with_import_root(path)
        return configuration.apply_runtime(environment)


@dataclass(frozen=True)
class TargetRuntime(RuntimeStack):
    """A runtime built from the target tree: its worker declares the tree's carry steps."""

    program: TargetProgram = field(kw_only=True)
