"""The development interpreter inherits a built runtime, never another lock solve."""
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
import shutil

import pytest
import tomli_w

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.building_runtime import BuildingRuntime
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.probes.packages import GitRecord, SourceRecord
from agent_comms.installer.runtime_home import RuntimeHome
from agent_comms.installer.runtime_resolution import CorePackage
from agent_comms.installer.runtime_stack import RuntimeStack
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo


@dataclass(frozen=True)
class FixtureProject:
    path: Path
    name: str
    module: str
    version: str = "1.0"

    def write(self, body: str, dependencies: tuple[str, ...] = ()) -> None:
        module = self.path / self.module
        module.mkdir(parents=True, exist_ok=True)
        (module / "__init__.py").write_text(body)
        (self.path / "pyproject.toml").write_text(tomli_w.dumps({
            "build-system": {"requires": ["hatchling"], "build-backend": "hatchling.build"},
            "project": {"name": self.name, "version": self.version,
                        "requires-python": ">=3.14", "dependencies": list(dependencies)},
            "tool": {"hatch": {"build": {"targets": {"wheel": {"packages": [self.module]}}}}},
        }))

    def commit(self, body: str) -> str:
        self.write(body)
        Repo.git(self.path, "add", ".")
        Repo.git(self.path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org",
                 "commit", "-qm", body)
        return Repo.git(self.path, "rev-parse", "HEAD").strip()


@dataclass(frozen=True)
class InstalledPair:
    live: Checkout
    staged: Path
    before: RuntimeStack
    after: RuntimeStack
    old_commit: str
    new_commit: str


@pytest.fixture
def installed_pair(tmp_path: Path, test_uv: Path) -> Iterator[InstalledPair]:
    plugin = FixtureProject(tmp_path / "plugin", "sync-probe", "sync_probe")
    plugin.path.mkdir()
    Repo.git(plugin.path, "init", "-q")
    old = plugin.commit("value = 'old'\n")
    new = plugin.commit("value = 'new'\n")
    home = tmp_path / "home"
    uv = RuntimeHome.uv_path(home)
    uv.parent.mkdir(parents=True)
    uv.symlink_to(test_uv)
    live = Checkout(tmp_path / "live checkout")
    staged = tmp_path / "staged"
    with InstallCommands() as commands:
        stacks = []
        for path, revision in ((live.path, old), (staged, new)):
            core = FixtureProject(path, CorePackage.distribution, "fixture_core")
            core.write("value = 'editable core'\n", (f"{plugin.name}=={plugin.version}",))
            stack = path / "stack"
            stack.mkdir()
            (stack / "pyproject.toml").write_text(tomli_w.dumps({
                "project": {"name": "fixture-stack", "version": "1",
                            "requires-python": ">=3.14", "dependencies": [core.name]},
                "tool": {"uv": {"package": False, "override-dependencies": [plugin.name], "sources": {
                    core.name: {"path": "..", "editable": True},
                    plugin.name: {"git": plugin.path.as_uri(), "rev": revision},
                }}},
            }))
            runtime = RuntimeStack(path, commands, home,
                                   runtime=LaunchRuntime.at_prefix(tmp_path / path.name / "built"))
            commands.run(uv, "lock", "--project", "stack", cwd=path)
            runtime.sync()
            stacks.append(runtime)
        before, after = stacks
        try:
            before.sync_test_environment(live)
            yield InstalledPair(live, staged, before, after, old, new)
        finally:
            for runtime in stacks:
                BuildingRuntime.discard(runtime.runtime.prefix)


@pytest.mark.parametrize("rollback", [False, True])
def test_development_uses_selected_runtime_not_either_checkout_lock(
    installed_pair: InstalledPair, rollback: bool,
):
    pair = installed_pair
    selected = pair.before.runtime if rollback else pair.after.runtime
    expected_commit = pair.old_commit if rollback else pair.new_commit
    expected_value = "old" if rollback else "new"
    # Rollback also has the incoming source tree, but selects the outgoing runtime.
    stack = RuntimeStack(pair.staged, pair.after.commands, pair.after.home, runtime=selected)
    lock = pair.live.path / "stack/uv.lock"
    original = lock.read_bytes()
    stack.sync_test_environment(pair.live)
    assert lock.read_bytes() == original
    shutil.rmtree(pair.staged / "fixture_core")
    python = LaunchRuntime.at_prefix(pair.live.test_environment).interpreter
    value, plugin_origin, core_origin, core_path = stack.commands.run(
        python, "-c",
        "from importlib.metadata import distribution; import sync_probe, fixture_core; "
        "print(sync_probe.value); print(distribution('sync-probe').read_text('direct_url.json')); "
        f"print(distribution({CorePackage.distribution!r}).read_text('direct_url.json')); "
        "print(fixture_core.__file__)", cwd=pair.live.path,
    ).splitlines()
    assert value == expected_value
    assert GitRecord.model_validate_json(plugin_origin).revision() == expected_commit
    assert SourceRecord.model_validate_json(core_origin).url == pair.live.path.as_uri()
    assert Path(core_path).is_relative_to(pair.live.path)


def test_failed_runtime_inventory_does_not_touch_development(
    installed_pair: InstalledPair, monkeypatch: pytest.MonkeyPatch,
):
    pair = installed_pair
    environment = pair.live.test_environment
    marker = environment / "keep"
    marker.write_text("existing development environment")

    def refuse(*command: str | Path, **options: object) -> str:
        raise RuntimeError("inventory unavailable")

    monkeypatch.setattr(pair.after.commands, "run", refuse)
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        pair.after.sync_test_environment(pair.live)
    assert marker.read_text() == "existing development environment"
    assert not tuple(pair.after.uv.parent.glob(".development-*"))
