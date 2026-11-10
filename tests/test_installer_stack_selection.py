"""Choose the complete build before starting the target's installer."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile

import pytest

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.interpreter_operation import TargetProgram
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.runtime_home import RuntimeHome
from agent_comms.installer.runtime_resolution import (
    CorePackage,
    ProjectMetadata,
    RuntimeResolution,
    ToadPackage,
)
from agent_comms.installer.runtime_stack import (
    HistoricalRuntime,
    HistoricalRuntimeBuild,
    MainRuntimeBuild,
    RuntimeBuild,
    RuntimeStack,
    TargetRuntime,
)
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo


@dataclass(frozen=True)
class BuildRepository:
    path: Path
    previous: str
    main: str
    candidate: str

    def select(self, revision: str) -> RuntimeBuild:
        Repo.git(self.path, "switch", "--detach", revision)
        return RuntimeBuild.select(Checkout(self.path), InstallCommands())


@pytest.fixture
def build_repository(tmp_path: Path) -> BuildRepository:
    path = tmp_path / "source"
    path.mkdir()
    Repo.git(path, "init", "--initial-branch=main")
    Repo.git(path, "config", "user.name", "Build test")
    Repo.git(path, "config", "user.email", "build@example.invalid")
    commits = []
    for value in ("previous", "main", "candidate"):
        (path / "value").write_text(value)
        Repo.git(path, "add", "value")
        Repo.git(path, "commit", "-m", value)
        commits.append(Repo.git(path, "rev-parse", "HEAD").strip())
    previous, main, candidate = commits
    Repo.git(path, "update-ref", "refs/remotes/origin/main", main)
    return BuildRepository(path, previous, main, candidate)


def test_main_build_selects_current_packages(build_repository: BuildRepository) -> None:
    assert isinstance(build_repository.select(build_repository.main), MainRuntimeBuild)


def test_historical_build_does_not_follow_current_branches(
    build_repository: BuildRepository,
) -> None:
    assert isinstance(build_repository.select(build_repository.previous), HistoricalRuntimeBuild)


def test_local_candidate_is_not_mistaken_for_historical_recovery(
    build_repository: BuildRepository,
) -> None:
    assert isinstance(build_repository.select(build_repository.candidate), MainRuntimeBuild)


def test_missing_main_does_not_guess_a_build_policy(build_repository: BuildRepository) -> None:
    Repo.git(build_repository.path, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(UpgradeRefused, match="origin/main"):
        build_repository.select(build_repository.previous)


def test_historical_build_keeps_required_client_console_scripts(tmp_path: Path) -> None:
    """Replacing Core alone must refuse before copying a prefix without bin/toad."""
    wheel = tmp_path / "agent_comms-1-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("agent_comms-1.dist-info/METADATA",
                         "Metadata-Version: 2.4\nName: agent-comms\nVersion: 1\n")
    runtime = LaunchRuntime.running()
    destination = tmp_path / "target"
    with InstallCommands() as commands:
        target = HistoricalRuntime(
            checkout=tmp_path, commands=commands, home=tmp_path,
            runtime=LaunchRuntime.at_prefix(destination),
            program=TargetProgram(tmp_path / "steps"),
            interpreter=runtime, source_runtime=runtime, wheels=(wheel,))
        with pytest.raises(UpgradeRefused, match="console-script package"):
            target.sync()
    assert not destination.exists()


@dataclass(frozen=True, kw_only=True)
class CheckedTarget(TargetRuntime):
    events: list[str]

    def complete(self) -> None:
        self.events.append("checked")


class RefusedTarget(CheckedTarget):
    def complete(self) -> None:
        super().complete()
        raise UpgradeRefused("client imports failed")


@dataclass(frozen=True, kw_only=True)
class ObservedBuild(RuntimeBuild):
    target: type[CheckedTarget]
    events: list[str]

    def stage(self, source: RuntimeStack, core: str) -> TargetRuntime:
        self.events.append("staged")
        return source.extended(self.target, events=self.events,
                               program=TargetProgram(self.checkout.path / "steps"))


@pytest.mark.parametrize("target", [CheckedTarget, RefusedTarget])
def test_build_checks_the_selected_packages_before_returning(
    tmp_path: Path, test_uv: Path, target: type[CheckedTarget],
) -> None:
    home = tmp_path / "home"
    uv = RuntimeHome.uv_path(home)
    uv.parent.mkdir(parents=True)
    uv.symlink_to(test_uv)
    repository = Checkout(Path(__file__).resolve().parents[1])
    events: list[str] = []
    with InstallCommands() as commands:
        build = ObservedBuild(repository, commands, home, target=target, events=events)
        if target is RefusedTarget:
            with pytest.raises(UpgradeRefused, match="client imports failed"):
                build.build()
        else:
            built = build.build()
            assert built.checkout == repository.path
    assert events == ["staged", "checked"]


@dataclass
class ToolCommands(InstallCommands):
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, *command: str | Path, **options: object) -> str:
        self.calls.append(tuple(map(str, command)))
        return ""


def test_client_repair_passes_exact_git_inputs_to_nra(tmp_path: Path) -> None:
    wheel = tmp_path / "agent_comms-1-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("agent_comms-1.dist-info/METADATA",
                         "Metadata-Version: 2.4\nName: agent-comms\nVersion: 1\n"
                         "Requires-Dist: nominal-refactor-advisor @ git+https://example.invalid/nra@main\n")
    core = tmp_path / "core"
    client = tmp_path / "client"
    with ToolCommands() as commands:
        build = HistoricalRuntimeBuild(Checkout(core), commands, tmp_path)
        source = RuntimeStack(core, commands, tmp_path, runtime=LaunchRuntime.running())
        build.repair_client(source, "a" * 40, "b" * 40, wheel, client, tmp_path)
    plan = json.loads((tmp_path / "client-imports.plan.json").read_text())
    assert plan["recipes"][0]["operations"] == [{
        "operation": "replay_git_module_imports",
        "provider_repository": str(core),
        "source_revision": "a" * 40,
        "target_revision": "b" * 40,
        "caller_root": str(client),
    }]
    (command,) = commands.calls
    assert command[1:6] == ("tool", "run", "--python", str(source.python), "--refresh-package")
    assert "nominal-refactor-advisor @ git+https://example.invalid/nra@main" in command
    assert command[-1] == "--json"


def dependency_wheel(
    directory: Path, name: str, version: str, module: str,
    requirements: tuple[str, ...] = (),
) -> Path:
    """Build a small wheel that uv can really install."""
    normalized = name.replace("-", "_")
    info = f"{normalized}-{version}.dist-info"
    wheel = directory / f"{normalized}-{version}-py3-none-any.whl"
    entries = {
        f"{module}/__init__.py": "",
        f"{info}/METADATA": (
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements)
        ),
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: dependency-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    entries[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in entries) + f"{info}/RECORD,,\n"
    with ZipFile(wheel, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return wheel


@pytest.mark.skipif(
    not RuntimeResolution.record_path().is_file(),
    reason="copying an installed build needs its recorded runtime",
)
def test_historical_runtime_installs_new_declared_dependency(tmp_path: Path, test_uv: Path) -> None:
    """Install a newly required package after copying the old packages."""
    donor = LaunchRuntime.installed()
    recorded = RuntimeResolution.at(donor.prefix)
    added = dependency_wheel(tmp_path, "historical-added-dependency", "1.0", "historical_added_dependency")
    requirement = f"{ProjectMetadata.from_wheel(added).name} @ {added.as_uri()}"
    wheels = tuple(
        dependency_wheel(tmp_path, package.distribution,
                         recorded.packages[package.distribution].version,
                         module, (requirement,))
        for package, module in ((CorePackage, CorePackage.__module__.partition(".")[0]),
                                (ToadPackage, ToadPackage.import_root))
    )
    home = tmp_path / "home"
    uv = RuntimeHome.uv_path(home)
    uv.parent.mkdir(parents=True)
    uv.symlink_to(test_uv)
    with InstallCommands() as commands:
        target = HistoricalRuntime(
            checkout=Path(__file__).resolve().parents[1], commands=commands, home=home,
            runtime=LaunchRuntime.at_prefix(tmp_path / "historical"),
            program=TargetProgram(tmp_path / "steps"), interpreter=donor,
            source_runtime=donor, wheels=wheels,
        )
        target.sync()
        assert (target.runtime.purelib / "historical_added_dependency" / "__init__.py").is_file()
        assert RuntimeResolution.at(donor.prefix) == recorded
