"""Actual standard backend output owns package facts; no patched metadata producers."""

from collections.abc import Callable
from pathlib import Path
import sys
import tomllib
from zipfile import ZipFile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
import pytest
import tomli_w

from agent_comms.head_review import BaseSide, HeadSide, IntegrationInput
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.runtime_resolution import ProjectMetadata, StackSources
from agent_comms.repository_name import RepositoryName
from github_fixture import GitHubFixture
from integration_source_fixture import MutualSources
from module_layers import MaterializedClients
from source_revision_fixture import SourceRepository

pytest_plugins = ("github_fixture", "test_holder_standing_rules", "integration_source_fixture")


def package(root: Path, name: str, *, dependencies: tuple[str, ...] = (),
            extras: dict[str, list[str]] | None = None, python: str = ">=3.10") -> SourceRepository:
    source = SourceRepository.create(root, RepositoryName.from_wire(f"example/{name}"), baseline_files={
        Path("pyproject.toml"): SourceRepository.package_project(
            name, dependencies=dependencies, extras=extras, python=python),
        Path("src/module.py"): "value = 1\n",
    })
    source.git(source.checkout, "switch", "feature")
    return source


@pytest.mark.parametrize("backend", ("hatch", "poetry"))
def test_standard_backend_metadata_preserves_identity_python_and_all_optional_edges(
    tmp_path: Path, test_uv: Path, backend: str,
) -> None:
    source = package(tmp_path / "source", "source-package", dependencies=("packaging>=24",),
                     extras={"syntax": ["idna>=3.9; python_version >= '3.10'"]})
    if backend == "poetry":
        source.publish_change("Use the declared Poetry backend, without a project table", *source.write_files({
            Path("pyproject.toml"): tomli_w.dumps({
                "build-system": {"requires": ["poetry-core>=1.2.0"], "build-backend": "poetry.core.masonry.api"},
                "tool": {"poetry": {"name": "source-package", "version": "0.0.1",
                    "description": "Real backend control", "authors": ["Source <source@example.invalid>"],
                    "packages": [{"include": "module.py", "from": "src"}],
                    "dependencies": {"python": ">=3.10", "packaging": ">=24",
                                     "idna": {"version": ">=3.9", "optional": True, "python": ">=3.10"}},
                    "extras": {"syntax": ["idna"]}}},
            })}))
    with InstallCommands() as commands:
        metadata = ProjectMetadata.acquire(source.checkout, uv=test_uv, python=Path(sys.executable),
                                           commands=commands, output=tmp_path / "metadata")
    assert metadata.name == "source-package" and metadata.extras == frozenset({"syntax"})
    assert len(metadata.requirements) == 2
    optional, = (edge for edge in metadata.requirements if edge.name == "idna")
    assert optional.marker is not None and "extra" in str(optional.marker)
    assert not optional.marker.evaluate({"extra": ""}) and optional.marker.evaluate({"extra": "syntax"})
    StackSources.require_package(metadata, "source_package")
    with pytest.raises(ValueError, match="not stack package"):
        StackSources.require_package(metadata, "different-package")
    with pytest.raises(ValueError, match="common interpreter"):
        ProjectMetadata.require_python(SpecifierSet("<1"))
    environment = StackSources.marker_environment(Path(sys.executable))
    with pytest.raises(ValueError, match="selected common interpreter"):
        StackSources.marker_environment(tmp_path / "different-interpreter")
    root = {"project": {"dependencies": ["source-package", "inactive; python_version < '1'"]},
            "tool": {"uv": {"package": False}}}
    assert all(edge.name != "inactive" for edge in StackSources.active_requirements(root, (metadata,), environment))
    assert all(edge.name != "idna" for edge in StackSources.active_requirements(root, (metadata,), environment))
    root["project"]["dependencies"] = ["source-package[syntax]"]
    assert optional in StackSources.active_requirements(root, (metadata,), environment)
    assert len(metadata.requirements) == 2  # Inactive edges were excluded from selection, never erased.


def test_runtime_extra_closure_keeps_bootstrap_only_changes_outside_runtime(
    tmp_path: Path, test_uv: Path,
) -> None:
    front = package(tmp_path / "front", "front", extras={"image": ["peer[paint]"]})
    before = package(tmp_path / "before", "peer", extras={
        "paint": ["front[image]", "idna>=3.9"], "checking": ["packaging>=24"]})
    after = package(tmp_path / "after", "peer", extras={
        "paint": ["front[image]", "idna>=3.10"], "checking": ["packaging>=25"]})
    with InstallCommands() as commands:
        front_metadata, before_metadata, after_metadata = (
            ProjectMetadata.acquire(source.checkout, uv=test_uv, python=Path(sys.executable),
                                    commands=commands, output=tmp_path / "metadata")
            for source in (front, before, after))
    environment = StackSources.marker_environment(Path(sys.executable))
    root = {"project": {"dependencies": ["front"]}, "tool": {"uv": {"package": False}}}
    before_active = StackSources.active_requirements(root, (front_metadata, before_metadata), environment)
    after_active = StackSources.active_requirements(root, (front_metadata, after_metadata), environment)
    assert before_active == after_active == (Requirement("front"),)
    root["project"]["dependencies"] = ["front[image]"]
    before_active = StackSources.active_requirements(root, (front_metadata, before_metadata), environment)
    after_active = StackSources.active_requirements(root, (front_metadata, after_metadata), environment)
    assert Requirement('peer[paint]; extra == "image"') in before_active
    assert any(edge.name == "idna" for edge in before_active)
    assert not any(edge.name == "packaging" for edge in before_active)
    assert before_active != after_active
    root["project"]["dependencies"] = ["peer[checking]"]
    assert StackSources.active_requirements(root, (before_metadata,), environment) != StackSources.active_requirements(
        root, (after_metadata,), environment)


def test_materialized_metadata_is_built_once_and_revalidated_when_borrowed(
    mutual_sources: MutualSources, github_cli: GitHubFixture,
    materialized_clients: Callable[[Path, IntegrationInput], MaterializedClients],
) -> None:
    pair = mutual_sources
    inputs = pair.capture(("P2", "P3"), "backend metadata lifetime")
    resource = materialized_clients(pair.output, inputs)
    metadata = resource.metadata
    built = len(resource.commands.clock.phases)
    assert built == sum(len(projects) for projects in metadata.values())
    for side in (BaseSide, HeadSide):
        assert resource.projects(side) is metadata[side]
        resource.cuts[side]
    assert resource.metadata is metadata and len(resource.commands.clock.phases) == built
    checkout = HeadSide.checkout(pair.output, pair.client.github)
    (checkout / "src/workflow.py").write_text("dirty = True\n")
    with pytest.raises(ValueError, match="dirty materialized"):
        resource.projects(HeadSide)


@pytest.mark.parametrize("mutation", ("dirty", "moved"))
def test_backend_cannot_mutate_the_captured_source(tmp_path: Path, test_uv: Path, mutation: str) -> None:
    source = package(tmp_path / "source", "source-package")
    document = tomllib.loads((source.checkout / "pyproject.toml").read_text())
    document["tool"]["hatch"]["build"]["hooks"] = {"custom": {"path": "hatch_build.py"}}
    action = '(Path(self.root) / "src/module.py").write_text("changed = True\\n")'
    if mutation == "moved":
        action += ('\n        subprocess.run(["git", "-C", self.root, "-c", "user.name=Source", '
                   '"-c", "user.email=source@example.invalid", "commit", "-am", "Backend moved source"], check=True)')
    hook = ('from pathlib import Path\nimport subprocess\n'
            'from hatchling.builders.hooks.plugin.interface import BuildHookInterface\n'
            'class Hook(BuildHookInterface):\n'
            '    def initialize(self, version, build_data):\n        ' + action + '\n')
    source.publish_change("Declare a real mutating backend hook", *source.write_files({
        Path("pyproject.toml"): tomli_w.dumps(document), Path("hatch_build.py"): hook}))
    with InstallCommands() as commands, pytest.raises(ValueError, match="moved or dirtied"):
        ProjectMetadata.acquire(source.checkout, uv=test_uv, python=Path(sys.executable),
                                commands=commands, output=tmp_path / "metadata")


def test_backend_failure_is_not_empty_metadata(tmp_path: Path, test_uv: Path) -> None:
    source = package(tmp_path / "source", "source-package")
    document = tomllib.loads((source.checkout / "pyproject.toml").read_text())
    document["project"]["version"] = "not-a-version"
    source.publish_change("Declare invalid backend metadata", *source.write_files({
        Path("pyproject.toml"): tomli_w.dumps(document)}))
    with InstallCommands() as commands, pytest.raises(UpgradeRefused):
        ProjectMetadata.acquire(source.checkout, uv=test_uv, python=Path(sys.executable),
                                commands=commands, output=tmp_path / "metadata")


@pytest.mark.parametrize("corruption", ("missing", "duplicate", "name", "header"))
def test_wheel_decoder_refuses_malformed_real_backend_output(
    tmp_path: Path, test_uv: Path, corruption: str,
) -> None:
    source = package(tmp_path / "source", "source-package")
    output = tmp_path / "wheel"
    with InstallCommands() as commands:
        commands.run(test_uv, "build", "--wheel", "--out-dir", output, "--python", Path(sys.executable), source.checkout)
    wheel, = output.glob("*.whl")
    with ZipFile(wheel) as archive:
        entries = [(entry, archive.read(entry)) for entry in archive.infolist()]
    with ZipFile(wheel, "w") as archive:
        for entry, data in entries:
            if entry.filename.endswith(".dist-info/METADATA"):
                if corruption == "missing":
                    continue
                if corruption == "name":
                    data = data.replace(b"Name: source-package", b"Name: different-package")
                if corruption == "header":
                    data = data.replace(b"Name: source-package", b"Name: source-package\nName: source-package")
                if corruption == "duplicate":
                    archive.writestr(entry, data)
            archive.writestr(entry, data)
    with pytest.raises(ValueError):
        ProjectMetadata.from_wheel(wheel)
