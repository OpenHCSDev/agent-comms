"""A real gh subprocess with controlled foreign responses and retained call records."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import ListedPullRequest, UnmergedListedPullRequest


@dataclass(frozen=True)
class GitHubFixture:
    directory: Path

    @property
    def path(self) -> Path:
        return self.directory / "pull_requests.json"

    def configure(self, *repositories: RepositoryName):
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        for repository in repositories:
            data.setdefault(repository.to_wire(), {
                "open_kinds": [kind.declared_name for kind in
                               UnmergedListedPullRequest.members_with(UnmergedListedPullRequest)],
                "requests": {},
            })
        self.path.write_text(json.dumps(data))
        return self

    def put(self, repository: RepositoryName, row: ListedPullRequest, body: str = "") -> None:
        data = json.loads(self.path.read_text())
        data[repository.to_wire()]["requests"][str(row.number)] = {"body": body, "listed": FieldCodec.encode(row)}
        self.path.write_text(json.dumps(data))

    def calls(self) -> tuple[tuple[str, ...], ...]:
        path = self.directory / "calls.jsonl"
        return tuple(tuple(json.loads(line)) for line in path.read_text().splitlines()) if path.exists() else ()


@pytest.fixture
def github_cli(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    (directory / "gh").symlink_to(Path(__file__).with_name("github_cli_stub.py"))
    monkeypatch.setenv("PATH", os.pathsep.join((str(directory), os.environ["PATH"])))
    return GitHubFixture(directory)
