"""One real Git/forge/Todo setup for matched source capture and promotion controls."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
import sys
import time

import pytest

from agent_comms.commander.order import Commander, CommanderContext
from agent_comms.comms import Comms
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.head_review import CombinedInput, IntegrationInput, IntegrationSource, PullRequestHead
from agent_comms.integration import IntegrationCheckout, IntegrationRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import DraftListedPullRequest, PullRequests, RecordPullRequest
from fixture_git import ENVIRONMENT
from github_fixture import GitHubFixture
from source_revision_fixture import SourceRepository
from module_layers import MaterializedClients


@dataclass(frozen=True)
class MutualSources:
    context: CommanderContext
    core: SourceRepository
    client: SourceRepository
    checkout: IntegrationCheckout
    output: Path
    github: GitHubFixture
    carrier_number: int = 101

    def publish_carrier(self, github: GitHubFixture) -> PullRequestHead:
        """Publish the separate combined PR; source-member refs and Todo links stay intact."""
        commit = self.core.git(self.checkout.path, "rev-parse", "HEAD").strip()
        self.core.git(self.checkout.path, "push", "origin", f"{commit}:refs/pull/{self.carrier_number}/head")
        stamp = "2026-10-24T00:00:00Z"
        github.put(self.core.github, DraftListedPullRequest(
            self.carrier_number, "combined", commit, stamp, "main", reviewable_times=(stamp,),
        ))
        return PullRequestHead(self.core.github, self.carrier_number, commit)

    def prepare_carrier(self, problems: tuple[str, ...]) -> PullRequestHead:
        """Fixture setup owns an actual published combination before production capture."""
        todos = tuple(self.context.comms.goals.todos.get(problem) for problem in problems)
        linked = PullRequests.linked_among(todos, self.core.github)
        heads = tuple(PullRequestHead(self.core.github, row.number, row.head)
                      for row in PullRequests(todos).rows(self.core.github, tuple(sorted(linked)))
                      if row.as_unmerged())
        self.checkout.combine_sources(IntegrationSource.main_head(self.core), heads)
        return self.publish_carrier(self.github)

    def capture(self, problems: tuple[str, ...], risk: str, *,
                companions: tuple[PullRequestHead, ...] = ()) -> CombinedInput:
        return IntegrationRun(self.checkout, self.output, risk).qualify(
            self.context, problems, carrier=self.prepare_carrier(problems), companions=companions)


@pytest.fixture
def source_pair(
    held_bus: Comms,
    configured_repository: Callable[[Comms, *tuple[Repo, ...]], Commander],
    github_cli: GitHubFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> MutualSources:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    client = SourceRepository.create(
        tmp_path / "matched-client", RepositoryName.from_wire("example/client"), baseline_files={
            Path("pyproject.toml"): SourceRepository.package_project("source-client"),
            Path("src/workflow.py"): 'from agent_comms.api import old_api\n\ndef run() -> str:\n    return old_api()\n',
        })
    core = SourceRepository.create(tmp_path / "matched-core", held_bus.goals.todos.get("P2").repo, baseline_files={
        Path("pyproject.toml"): SourceRepository.package_project("agent-comms"),
        Path("stack/pyproject.toml"): (
            '[project]\nname = "fixture-stack"\nversion = "0.0.1"\ndependencies = ["agent-comms", "source-client"]\n'
            '[tool.uv]\npackage = false\noverride-dependencies = ["packaging==26.0"]\n'
            'constraint-dependencies = ["typing-extensions>=4.15"]\n'
            '[tool.uv.pip]\nstrict = true\n'
            '[tool.uv.sources]\nagent-comms = {path = "..", editable = true}\n'
            f'source-client = {{git = "{client.remote_path.as_uri()}", branch = "main"}}\n'
        ),
        Path("stack/uv.lock"): 'version = 1\nrevision = 3\nrequires-python = ">=3.14"\npackage = []\n',
        Path("uv.lock"): 'version = 1\nrevision = 3\nrequires-python = ">=3.14"\npackage = []\n',
        Path("src/agent_comms/__init__.py"): "",
        Path("src/agent_comms/api.py"): 'def old_api() -> str:\n    return "baseline"\n',
    })
    for source in (core, client):
        source.git(source.checkout, "switch", "feature")
    core.publish_change("Replace the Core export without an alias", *core.write_files({
        Path("src/agent_comms/api.py"): 'def new_api() -> str:\n    return "candidate"\n',
    }))
    client.publish_change("Migrate the client to the new Core export", *client.write_files({
        Path("src/workflow.py"): 'from agent_comms.api import new_api\n\ndef run() -> str:\n    return new_api()\n',
    }))
    context = configured_repository(held_bus, core, client)
    primary = held_bus.goals.todos.get("P2")
    held = primary.held()
    RecordPullRequest(primary.revision, held.owner_identity, core.link, held.generation).record(
        held_bus.goals, primary.id)
    stamp = "2026-10-24T00:00:00Z"
    for source in (core, client):
        github_cli.put(source.github, DraftListedPullRequest(
            source.number, "feature", source.branch_commit, stamp, "main", reviewable_times=(stamp,),
        ))
    checkout = IntegrationCheckout.create(core, tmp_path / "qualification", core.main_commit)
    return MutualSources(context, core, client, checkout, tmp_path / "qualified-inputs", github_cli)


@pytest.fixture
def materialized_clients(test_uv: Path) -> Callable[[Path, IntegrationInput], MaterializedClients]:
    """The root test run supplies the frontend; metadata acquisition owns its command lifetime."""
    def acquire(directory: Path, inputs: IntegrationInput) -> MaterializedClients:
        return MaterializedClients(directory, inputs, test_uv, Path(sys.executable))
    return acquire


@pytest.fixture
def mutual_sources(source_pair: MutualSources) -> MutualSources:
    """The same source owner, with its companion linked by a separate real checked order."""
    comms = source_pair.context.comms
    primary = comms.goals.todos.get("P2")
    comms.goals.update_goal("worker", CreateProblemGoalAction(
        problem="P3", commander="lead", request=primary.request,
        assigner=comms.registry.require("lead").incarnation,
        repo=source_pair.client.github, text="Migrate the paired source client",
        order=replace(primary.order.checked(), pull_requests=(source_pair.client.link,),
                      check=replace(primary.order.check, directory=source_pair.client.checkout)),
        review_at=RecordedEventTime(time.time() + 3600),
    ))
    return source_pair
