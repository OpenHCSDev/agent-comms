"""Private full-project projection against real captured Git/forge inputs; no fake resolver."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path
import os
import sys
import tomllib

from packaging.requirements import Requirement
import pytest
import tomli_w

from agent_comms.head_review import BaseSide, HeadSide
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.runtime_resolution import CorePackage, GitSource, ResolvedLock
from agent_comms.integration import IntegrationRun
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import DraftListedPullRequest
from checker_stack import CheckerStack
from github_fixture import GitHubFixture
from integration_source_fixture import MutualSources
from source_revision_fixture import SourceRepository

pytest_plugins = ("test_holder_standing_rules", "integration_source_fixture")


@pytest.mark.parametrize("dependencies,allowed", [((), True), (("idna==3.10",), False)])
def test_private_project_preserves_settings_pins_and_refuses_external_migration(
    mutual_sources: MutualSources, github_cli: GitHubFixture, tmp_path: Path,
    dependencies: tuple[str, ...], allowed: bool, monkeypatch: pytest.MonkeyPatch, test_uv: Path,
) -> None:
    pair = mutual_sources
    # Edit the real source before capture, never rewrite the captured artifact or selectors.
    core_project = pair.core.checkout / "pyproject.toml"
    document = tomllib.loads(core_project.read_text())
    document["project"]["dependencies"] = list(dependencies)
    stack = tomllib.loads((pair.core.checkout / "stack/pyproject.toml").read_text())
    stack["project"]["python"] = "unmodeled convenience spelling"
    stack["tool"]["uv"]["overrides"] = ["unmodeled overrides"]
    stack["tool"]["uv"]["constraints"] = ["unmodeled constraints"]
    stack["tool"]["uv"]["indexes"] = [{"unmodeled": "indexes"}]
    stack["tool"]["fixture"] = {"dates": [date(2026, 10, 28)], "matrix": [{"enabled": False}]}
    stack["tool"]["uv"]["index"] = [{"name": "fixture-index", "url": "https://pypi.org/simple",
                                    "explicit": True, "authenticate": "always"}]
    stack["tool"]["uv"]["sources"]["conditional-package"] = [
        {"index": "fixture-index", "marker": "sys_platform == 'linux'"}]
    head = pair.core.publish_change("Declare this candidate runtime contract", *pair.core.write_files({
        Path("pyproject.toml"): tomli_w.dumps(document),
        Path("stack/pyproject.toml"): tomli_w.dumps(stack)}))
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(pair.core.github, DraftListedPullRequest(
        pair.core.number, "feature", head, stamp, "main", reviewable_times=(stamp,)))
    inputs = IntegrationRun(pair.checkout, pair.output, "full private checker project").qualify(pair.context, ("P2", "P3"))
    with InstallCommands() as commands:
        checker = CheckerStack(checkout=tmp_path / "checker", commands=commands, home=tmp_path,
                               runtime=LaunchRuntime.running(), source_directory=pair.output,
                               inputs=inputs, uv_program=test_uv)
        original = checker.core / "stack"
        before = (original / "pyproject.toml").read_bytes(), (original / "uv.lock").read_bytes()
        if allowed:
            checker.write_project()
            expected = tomllib.loads(before[0].decode())
            expected["tool"]["uv"]["sources"]["agent-comms"] = {"path": str(checker.core), "editable": True}
            expected["tool"]["uv"]["sources"]["source-client"] = {
                "path": str(HeadSide.checkout(pair.output, pair.client.github)), "editable": True}

            assert tomllib.loads((checker.checkout / "stack/pyproject.toml").read_text()) == expected
            assert checker.lock.read_bytes() == before[1]
            monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
            with pytest.raises(ValueError, match="isolated runner bootstrap prefix"):
                checker.prepare()
        else:
            with pytest.raises(ValueError, match="External runtime dependency migration"):
                checker.write_project()
            assert not checker.checkout.exists()
        assert ((original / "pyproject.toml").read_bytes(), (original / "uv.lock").read_bytes()) == before
        for side in (BaseSide, HeadSide):
            for source in inputs.sources:
                side.require_materialized(pair.output, source)


@pytest.mark.parametrize("requested", (False, True))
def test_bootstrap_extra_is_runtime_only_when_the_virtual_root_requests_it(
    mutual_sources: MutualSources, github_cli: GitHubFixture, tmp_path: Path,
    requested: bool, test_uv: Path,
) -> None:
    pair = mutual_sources
    pair.core.git(pair.core.checkout, "switch", "main")
    project = tomllib.loads((pair.core.checkout / "pyproject.toml").read_text())
    project["project"]["optional-dependencies"] = {"checking": ["idna>=3.9"]}
    stack = tomllib.loads((pair.core.checkout / "stack/pyproject.toml").read_text())
    if requested:
        stack["project"]["dependencies"] = ["agent-comms[checking]", "source-client"]
    pair.core.commit_change("Declare actual baseline root and optional dependency", *pair.core.write_files({
        Path("pyproject.toml"): tomli_w.dumps(project),
        Path("stack/pyproject.toml"): tomli_w.dumps(stack)}))
    pair.core.git(pair.core.checkout, "push", "origin", "main")
    pair.core.git(pair.core.checkout, "switch", "feature")
    pair.core.git(pair.core.checkout, "merge", "--no-edit", "main")
    project["project"]["optional-dependencies"] = {"checking": ["idna>=3.10"]}
    head = pair.core.publish_change("Change only the optional checker dependency", *pair.core.write_files({
        Path("pyproject.toml"): tomli_w.dumps(project)}))
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(pair.core.github, DraftListedPullRequest(
        pair.core.number, "feature", head, stamp, "main", reviewable_times=(stamp,)))
    inputs = IntegrationRun(pair.checkout, pair.output, "runtime root extras, not bootstrap extras").qualify(
        pair.context, ("P2", "P3"))
    with InstallCommands() as commands:
        checker = CheckerStack(checkout=tmp_path / "checker", commands=commands, home=tmp_path,
            runtime=LaunchRuntime.running(), source_directory=pair.output, inputs=inputs, uv_program=test_uv)
        if requested:
            with pytest.raises(ValueError, match="External runtime dependency migration"):
                checker.require_runtime_compatibility()
        else:
            checker.require_runtime_compatibility()
        before = checker.clients.projects(BaseSide)[pair.core.github]
        after = checker.clients.projects(HeadSide)[pair.core.github]
        assert before.requirements != after.requirements  # Complete metadata was never filtered away.


@pytest.fixture
def common_checker(
    mutual_sources: MutualSources, github_cli: GitHubFixture, tmp_path: Path, test_uv: Path,
) -> Iterator[CheckerStack]:
    """Two genuine independently locked graphs, not hand-built lock/installed claims."""
    pair = mutual_sources
    plugin = SourceRepository.create(
        tmp_path / "plugin", RepositoryName.from_wire("example/checker-plugin"), baseline_files={
            Path("python package/pyproject.toml"): SourceRepository.package_project("checker-plugin"),
            Path("python package/src/checker_plugin.py"): "value = 1\n",
            Path("other package/pyproject.toml"): SourceRepository.package_project("checker-plugin"),
            Path("other package/src/checker_plugin.py"): "value = 2\n"})
    pair.client.git(pair.client.checkout, "switch", "main")
    pair.client.commit_change("Declare runtime-only dependency", *pair.client.write_files({
        Path("pyproject.toml"): pair.client.package_project("source-client", dependencies=(
            "charset-normalizer==3.4.0",))}))
    pair.client.git(pair.client.checkout, "push", "origin", "main")
    pair.client.git(pair.client.checkout, "switch", "feature")
    pair.client.git(pair.client.checkout, "merge", "--no-edit", "main")
    pair.core.git(pair.core.checkout, "switch", "main")
    pair.core.write_files({Path("pyproject.toml"): pair.core.package_project("agent-comms",
        dependencies=("idna>=3.10,<4; python_version >= '3.10'",), extras={
            "checking": ["idna<3.11", "six==1.16.0",
                         f"checker-plugin @ git+{plugin.remote.as_uri()}@main#subdirectory=python%20package"]})})
    stack = tomllib.loads((pair.core.checkout / "stack/pyproject.toml").read_text())
    stack["tool"]["uv"]["sources"]["source-client"]["git"] = pair.client.remote.as_uri()
    stack["project"]["dependencies"] = ["agent-comms>=0.0.1; python_version >= '3.10'", "source-client"]
    stack["tool"]["uv"]["override-dependencies"].append("agent-comms>=0.0.1; python_version >= '3.10'")
    pair.core.write_files({Path("stack/pyproject.toml"): tomli_w.dumps(stack)})
    with InstallCommands() as commands:
        for project in (pair.core.checkout, pair.core.checkout / "stack"):
            commands.run(test_uv, "lock", "--project", project, "--python", Path(sys.executable))
        pair.core.commit_change("Retain actual independent bootstrap and runtime locks", "pyproject.toml",
                                 "stack/pyproject.toml", "uv.lock", "stack/uv.lock")
        pair.core.git(pair.core.checkout, "push", "origin", "main")
        pair.core.git(pair.core.checkout, "switch", "feature")
        pair.core.git(pair.core.checkout, "merge", "--no-edit", "main")
        stamp = "2026-10-24T00:00:00Z"
        for source in (pair.core, pair.client):
            source.git(source.checkout, "push", "origin", f"HEAD:refs/pull/{source.number}/head")
            github_cli.put(source.github, DraftListedPullRequest(
                source.number, "feature", source.branch_commit, stamp, "main", reviewable_times=(stamp,)))
        inputs = IntegrationRun(pair.checkout, pair.output, "one complete runtime/checker solve").qualify(
            pair.context, ("P2", "P3"))
        checker = CheckerStack(checkout=tmp_path / "checker", commands=commands, home=tmp_path,
            runtime=LaunchRuntime.running(), source_directory=pair.output, inputs=inputs, uv_program=test_uv)
        checker.write_project()
        yield checker


def test_complete_project_reconciles_shared_preferences_and_keeps_both_cuts_on_one_prefix(
    common_checker: CheckerStack,
) -> None:
    checker = common_checker
    originals = {path: path.read_bytes() for side in (BaseSide, HeadSide)
        for path in (side.checkout(checker.source_directory, CorePackage.repository) / "uv.lock",
                     side.checkout(checker.source_directory, CorePackage.repository) / "stack/uv.lock")}
    bootstrap = checker.bootstrap_lock.external(checker.local_names)
    runtime = checker.runtime_locks[HeadSide].external(checker.local_names)
    assert bootstrap["idna"].version != runtime["idna"].version
    metadata = checker.clients.projects(HeadSide)[CorePackage.repository]
    private = tomllib.loads((checker.checkout / "stack/pyproject.toml").read_text())
    for roots in (private["project"]["dependencies"], private["tool"]["uv"]["override-dependencies"]):
        core, = (Requirement(text) for text in roots if Requirement(text).name == metadata.name)
        assert core.extras == metadata.extras and str(core.specifier) == ">=0.0.1"
        assert core.marker is not None and "python_version" in str(core.marker)
    assert not any(edge.name == "six" for edge in checker.requirements(HeadSide))
    assert any(edge.name == "six" for edge in checker.private_requirements())
    assert any(edge.marker is not None for edge in checker.private_requirements())
    source = private["tool"]["uv"]["sources"]["checker-plugin"]
    assert source["subdirectory"] == "python package"
    override, = (Requirement(text) for text in private["tool"]["uv"]["override-dependencies"]
                 if Requirement(text).name == "checker-plugin")
    assert override.url == f"git+{source['git']}@{source['rev']}#subdirectory=python%20package"
    for query in ("unknown=python", "subdirectory=python&subdirectory=other", "subdirectory=",
                  "branch=main&tag=release", "subdirectory=%00", "subdirectory=%ZZ", "branch="):
        with pytest.raises(ValueError, match="Git"):
            GitSource(git=f"{source['git']}?{query}#{source['rev']}").project_source()
    for revision in ("", "a" * 7, "Z" * 40):
        with pytest.raises(ValueError, match="Git"):
            GitSource(git=f"{source['git']}?branch=main#{revision}").project_source()
    checker.resolve()
    locked = ResolvedLock.read(checker.lock).external(checker.local_names)
    assert locked["idna"].version == bootstrap["idna"].version
    for name, package in checker.retained_packages.items():
        package.require_retained(locked[name])
    prefix = checker.home / "common-prefix"
    checker.commands.run(checker.uv, "venv", "--python", checker.python, prefix)
    checker.commands.run(checker.uv, "sync", "--project", checker.checkout / "stack", "--locked",
        "--no-editable", "--python", checker.python,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(prefix)})
    checker.commands.run(checker.uv, "pip", "check", "--python", prefix / "bin/python")
    for side, answer in ((BaseSide, "baseline"), (HeadSide, "candidate")):
        roots = [str(side.checkout(checker.source_directory, source.repository) / "src")
                 for source in checker.inputs.sources]
        # Both real API cuts execute with this same installed dependency prefix.
        result = checker.commands.run(prefix / "bin/python", "-c",
            "import runpy, sys, idna; sys.path[:0] = sys.argv[1:-1]; "
            "print(sys.prefix); print(idna.__version__); print(runpy.run_path(sys.argv[-1])['run']())",
            *roots, side.checkout(checker.source_directory, RepositoryName.from_wire("example/client"))
            / "src/workflow.py")
        assert result.splitlines() == [str(prefix), locked["idna"].version, answer]
    assert {path: path.read_bytes() for path in originals} == originals


def test_incompatible_full_root_graph_refuses_during_solve(common_checker: CheckerStack) -> None:
    checker = common_checker
    project = checker.checkout / "stack/pyproject.toml"
    document = tomllib.loads(project.read_text())
    document["project"]["dependencies"].append("idna>=3.11")
    project.write_text(tomli_w.dumps(document))
    with pytest.raises(UpgradeRefused):
        checker.resolve()
    assert not (checker.home / "common-prefix").exists()  # No sync or corrective install happened.


@pytest.mark.parametrize("package,change", (
    ("charset-normalizer", "3.4.1"), ("six", "1.17.0"),
    ("checker-plugin", "revision"), ("checker-plugin", "location"),
))
def test_retained_runtime_checker_version_and_git_identity_refuse_actual_solve_drift(
    common_checker: CheckerStack, package: str, change: str,
) -> None:
    checker = common_checker
    expected = checker.retained_packages[package]
    project = checker.checkout / "stack/pyproject.toml"
    document = tomllib.loads(project.read_text())
    uv = document["tool"]["uv"]
    uv["constraint-dependencies"] = [text for text in uv["constraint-dependencies"]
                                     if Requirement(text).name != package]
    if change in ("revision", "location"):
        # Move only the genuine external repository and solve its new exact source. The
        # retained canonical lock must reject a same-version, different-commit package.
        source = uv["sources"][package]
        remote = Path(source["git"].removeprefix("file://"))
        repository = SourceRepository.of(remote.parents[1] / "repository")
        if change == "revision":
            commit = repository.commit_change("Same version, different checker implementation", *repository.write_files({
                Path("python package/src/checker_plugin.py"): "value = 3\n"}))
            repository.git(repository.checkout, "push", "origin", "main")
            source["rev"] = commit
        else:
            source["subdirectory"] = "other package"  # Same repo/SHA/version, another actual project.
        location = source["subdirectory"].replace(" ", "%20")
        uv["override-dependencies"] = [f"{package} @ git+{source['git']}@{source['rev']}#subdirectory={location}"
            if Requirement(text).name == package else text for text in uv["override-dependencies"]]
    else:
        # Changing the explicit private override is not an accepted waiver: the retained
        # source/version owner still detects the resulting genuine solver selection.
        uv["override-dependencies"].append(f"{package}=={change}")
    project.write_text(tomli_w.dumps(document))
    checker.resolve()
    actual = ResolvedLock.read(checker.lock).external(checker.local_names)[package]
    with pytest.raises(ValueError, match="changed retained source/version"):
        expected.require_retained(actual)
