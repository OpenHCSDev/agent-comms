"""Private full-project projection against real captured Git/forge inputs; no fake resolver."""

from pathlib import Path
import tomllib

import pytest
import tomli_w

from agent_comms.head_review import BaseSide, HeadSide
from agent_comms.installer.commands import InstallCommands
from agent_comms.integration import IntegrationRun
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.todos import DraftListedPullRequest
from checker_stack import CheckerStack
from github_fixture import GitHubFixture
from integration_source_fixture import MutualSources

pytest_plugins = ("test_holder_standing_rules", "integration_source_fixture")


@pytest.mark.parametrize("dependencies,allowed", [((), True), (("idna==3.10",), False)])
def test_private_project_preserves_settings_pins_and_refuses_external_migration(
    mutual_sources: MutualSources, github_cli: GitHubFixture, tmp_path: Path,
    dependencies: tuple[str, ...], allowed: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = mutual_sources
    # Edit the real source before capture, never rewrite the captured artifact or selectors.
    core_project = pair.core.checkout / "pyproject.toml"
    document = tomllib.loads(core_project.read_text())
    document["project"]["dependencies"] = list(dependencies)
    head = pair.core.publish_change("Declare this candidate runtime contract", *pair.core.write_files({
        Path("pyproject.toml"): tomli_w.dumps(document)}))
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(pair.core.github, DraftListedPullRequest(
        pair.core.number, "feature", head, stamp, "main", reviewable_times=(stamp,)))
    inputs = IntegrationRun(pair.checkout, pair.output, "full private checker project").qualify(pair.context, ("P2", "P3"))
    with InstallCommands() as commands:
        checker = CheckerStack(checkout=tmp_path / "checker", commands=commands, home=tmp_path,
                               runtime=LaunchRuntime.running(), source_directory=pair.output,
                               inputs=inputs, uv_program=Path("uv"))
        original = checker.core / "stack"
        before = (original / "pyproject.toml").read_bytes(), (original / "uv.lock").read_bytes()
        if allowed:
            checker.write_project()
            expected = tomllib.loads(before[0].decode())
            expected["tool"]["uv"]["sources"] = {
                "agent-comms": {"path": str(checker.core), "editable": True},
                "source-client": {"path": str(HeadSide.checkout(pair.output, pair.client.github)), "editable": True},
            }
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
