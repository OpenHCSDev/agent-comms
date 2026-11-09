"""One real Git/forge/Todo setup for matched source capture and promotion controls."""

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self

import pytest
from module_layers import MaterializedClients

from agent_comms.comms import Comms
from agent_comms.projects.integration import IntegrationCheckout, IntegrationRun
from agent_comms.planning.goal_actions import CreateProblemGoalAction
from agent_comms.planning.orders import (
    DraftListedPullRequest,
    PullRequests,
    RecordPullRequest,
)
from agent_comms.projects.head_review import (
    CombinedInput,
    IntegrationInput,
    IntegrationSource,
    PullRequestHead,
)
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.team.order import Team, TeamContext
from agent_comms.transport.tool_caller import PeerCaller
from agent_comms.transport.tools.orders import CommsTodoCreateTool
from fixture_git import ENVIRONMENT
from github_fixture import GitHubFixture
from source_revision_fixture import SourceRepository


@dataclass(frozen=True)
class MutualSources:
    context: TeamContext
    core: SourceRepository
    client: SourceRepository
    checkout: IntegrationCheckout
    output: Path
    github: GitHubFixture
    carrier_number: int = 101

    @staticmethod
    def owned_checkout(context: TeamContext, repository: SourceRepository,
                       owner: str, problem: str) -> IntegrationCheckout:
        """Create at the real storage declaration, never patch checkout ownership."""
        holder = context.comms.registry.require(owner)
        storage = context.comms.registry.storage_rule
        path = storage.repository_checkout(storage.roots_of(holder), problem, repository.github)
        return IntegrationCheckout.create(repository, path, repository.main_commit)

    def link_companion(self, problem: str, source: SourceRepository, text: str) -> None:
        """Link a companion source by a separate real checked order.

        The worker's goal slot is occupied, so the worker accepts the companion itself."""
        comms = self.context.comms
        primary = comms.goals.todos.get("P2")
        creation = CreateProblemGoalAction(
            problem=problem, request=primary.request,
            assigner=comms.registry.require("colleague").incarnation,
            repo=source.github, text=text,
            order=replace(primary.order.checked(), pull_requests=(source.link,),
                          check=replace(primary.order.check, directory=source.checkout)),
            review_at=RecordedEventTime(time.time() + 3600),
        )
        PeerCaller("worker").invoke(CommsTodoCreateTool(owner="worker", order=creation), comms)

    def publish_carrier(self, github: GitHubFixture) -> PullRequestHead:
        """Publish the separate combined PR; source-member refs and Todo links stay intact."""
        commit = self.core.git(self.checkout.path, "rev-parse", "HEAD").strip()
        self.core.git(self.checkout.path, "push", "origin", f"{commit}:refs/pull/{self.carrier_number}/head")
        stamp = "2026-10-24T00:00:00Z"
        github.put(self.core.github, DraftListedPullRequest(
            self.carrier_number, "combined", commit, stamp, "main", reviewable_times=(stamp,),
        ))
        return PullRequestHead(self.core.github, self.carrier_number, commit)

    def starting_publication(self, reply_refusal: str, *, checkout_owner: str = "worker",
                             lag_reads: int = 0) -> Self:
        """The forge follows the pushed carrier branch, including an actually lost reply."""
        context = self.context.model_copy(update={"actor": PeerCaller("worker")})
        checkout = self.owned_checkout(context, self.core, checkout_owner, "P3")
        self.core.git(checkout.path, "switch", "-c", "publish-carrier")
        stamp = "2026-10-24T00:00:00Z"
        self.github.create_next(self.core.github, DraftListedPullRequest(
            self.carrier_number, "publish-carrier", self.core.main_commit, stamp, "main",
            reviewable_times=(stamp,)), remotes=(self.core.remote_path,),
            reply_refusal=reply_refusal, lag_reads=lag_reads)
        return replace(self, context=context, checkout=checkout)

    def publish(self, output: Path) -> CombinedInput:
        return IntegrationRun(self.checkout, output, "Actual companion-only first publication").publish(
            self.context, ("P3",), title="Publish the actual paired source cut",
            body="The client migration is substantive; Core retains its actual source cut.")

    @staticmethod
    def advance_main(*sources: SourceRepository) -> None:
        """One real main-advance procedure for the paired cut's refusal controls."""
        for source in sources:
            source.git(source.checkout, "switch", "main")
            source.commit_change("Advance actual main", *source.write_files({
                Path("main-only.py"): "advanced = True\n",
            }))
            source.git(source.checkout, "push", "origin", "main")
            source.git(source.checkout, "switch", "feature")

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
    configured_repository: Callable[[Comms, *tuple[Repo, ...]], Team],
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
        Path("pyproject.toml"): (
            SourceRepository.package_project("agent-comms")
            + '\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\npython_files = ["test_*.py"]\n'
        ),
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
    checkout = MutualSources.owned_checkout(context, core, "colleague", "P2")
    return MutualSources(context, core, client, checkout, tmp_path / "qualified-inputs", github_cli)


@pytest.fixture
def materialized_clients(test_uv: Path) -> Callable[[Path, IntegrationInput], MaterializedClients]:
    """The root test run supplies the frontend; metadata acquisition owns its command lifetime."""
    def acquire(directory: Path, inputs: IntegrationInput) -> MaterializedClients:
        return MaterializedClients(directory, inputs, test_uv, Path(sys.executable))
    return acquire


@pytest.fixture
def mutual_sources(source_pair: MutualSources) -> MutualSources:
    """The same source owner, with its client migration as a linked companion."""
    source_pair.link_companion("P3", source_pair.client, "Migrate the paired source client")
    return source_pair
