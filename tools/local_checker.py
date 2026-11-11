"""Build on this machine the checker environment CI builds for one tested commit.

`tools/local_checker.py pr N` or `tools/local_checker.py main`. The request CI's workflow
passes on each step's command line is constructed for a runner root under agent-scratch, and
CI's own steps then run in the tested checkout with that checkout's own code: the bootstrap
`uv sync`, the source resolution, and the checker build. The checker's interpreter is printed
last, for running tests in it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.projects import integration
from agent_comms.cli_verbs.declared_verb import DeclaredVerb
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.declared_uv import DeclaredUv
from agent_comms.projects.integration import (
    CiEvent,
    CiRunner,
    IntegrationFailed,
    MainSources,
    PullRequestSources,
    TestedSources,
)
from agent_comms.nominal.field_codec import Argument, FieldCodec
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.projects.work_branches import WorkBranches


@dataclass(frozen=True, kw_only=True)
class LocalCheckerVerb(DeclaredVerb, affix="Verb"):
    """One tested source CI builds a checker for, and the event CI's runner sees for it."""

    sources: ClassVar[type[TestedSources]]
    """The CI event member this verb reproduces."""

    @classmethod
    def tool(cls) -> str:
        return Path(__file__).name

    @property
    @abstractmethod
    def name(self) -> str:
        """This run's directory under the local runner base."""

    @property
    @abstractmethod
    def reference(self) -> str:
        """The forge ref whose commit CI checks out and tests."""

    @abstractmethod
    def document(self, repository: RepositoryName, tested: str) -> object:
        """The event document CI's runner hands its steps."""

    def request(self, root: Path, repository: RepositoryName, tested: str) -> CiEvent:
        """The request CI's workflow passes its steps, for a runner rooted at ``root``."""
        document = root / "event.json"
        document.write_text(json.dumps(self.document(repository, tested)))
        return CiEvent(
            root=root, repository=repository, tested=tested, event=self.sources, document=document
        )

    @staticmethod
    def step(command: tuple[str | Path, ...], checkout: Path, environment: dict[str, str]) -> None:
        print(f"LOCAL CI STEP {' '.join(map(str, command))}", flush=True)
        subprocess.run(command, cwd=checkout, env=environment, check=True)

    def build(self) -> Path:
        """Check out exactly what CI tests, then run CI's steps on it; returns the checker."""
        tool = Path(__file__).resolve().parents[1]
        repository = Repo.of(tool).github
        root = StorageRule().scratch_base / "local-ci" / self.name
        root.mkdir(parents=True, exist_ok=False)
        checkout = root / "checkout"
        # The tool's own object store spares the network every object it already holds.
        objects = Repo.git(tool, "rev-parse", "--path-format=absolute", "--git-common-dir")
        Repo.git(
            root,
            "clone",
            "--no-checkout",
            "--reference",
            objects.strip(),
            Repo.github_url(repository),
            str(checkout),
            timeout=600,
        )
        Repo.git(checkout, "fetch", "origin", self.reference, timeout=600)
        tested = Repo.git(checkout, "rev-parse", "FETCH_HEAD^{commit}").strip()
        Repo.git(checkout, "checkout", "--detach", tested)
        event = self.request(root, repository, tested)
        checker = LaunchRuntime.at_prefix(root / "checker-venv")
        uv = Path.home() / DeclaredUv.installed
        reported = subprocess.run((uv, "--version"), capture_output=True, text=True, check=True)
        if not DeclaredUv.of(checkout / "pyproject.toml", uv).accepts(reported.stdout):
            raise IntegrationFailed(f"{uv} is not the uv the tested checkout declares")
        self.step(
            (uv, "sync", "--locked", "--all-extras"),
            checkout,
            {**os.environ, "UV_PROJECT_ENVIRONMENT": str(checker.prefix)},
        )
        # CI puts the checker's bin first on PATH for every later step.
        environment = checker.environment(os.environ)
        sources = root / "paired-sources"
        self.step(
            (
                checker.interpreter,
                "-m",
                integration.__name__,
                "--resolve-ci-inputs",
                sources,
                *CiEvent.to_arguments(event),
            ),
            checkout,
            {**environment, "PYTHONPATH": "src"},
        )
        self.step(
            (
                checker.interpreter,
                "tools/checker_stack.py",
                "--ci-inputs",
                sources,
                *CiRunner.to_arguments(event),
            ),
            checkout,
            {**environment, "PYTHONPATH": "src:tools"},
        )
        return checker.interpreter


@dataclass(frozen=True, kw_only=True)
class PrVerb(LocalCheckerVerb):
    help = "Build the checker CI builds for a pull request's merge with main"
    sources = PullRequestSources
    number: Annotated[int, Argument(flags=("number",), options={"help": "the pull request"})]

    @property
    def name(self) -> str:
        return f"pr-{self.number}"

    @property
    def reference(self) -> str:
        return f"refs/pull/{self.number}/merge"

    def document(self, repository: RepositoryName, tested: str) -> object:
        response = repository.rest(f"pulls/{self.number}")
        if response.returncode:
            raise IntegrationFailed(response.stderr.strip() or f"Cannot read #{self.number}")
        return {"number": self.number, "pull_request": FieldCodec.parse(response.stdout)}


@dataclass(frozen=True, kw_only=True)
class MainVerb(LocalCheckerVerb):
    help = "Build the checker CI builds for main's latest merged pull request"
    sources = MainSources

    @property
    def name(self) -> str:
        return WorkBranches.main

    @property
    def reference(self) -> str:
        return f"refs/heads/{WorkBranches.main}"

    def document(self, repository: RepositoryName, tested: str) -> object:
        return {"ref": self.reference, "after": tested}


def main() -> int:
    print(LocalCheckerVerb.parse(sys.argv[1:]).build(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
