#!/usr/bin/env python3
"""One controlled gh boundary for copied-bus checks; never contacts GitHub.

The parent writes canonical GitHub response bodies at their API addresses. The
child serves those external responses using only the standard library: there is
no private fixture schema or second product decoder to load in every gh child.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from agent_comms.repository_name import RepositoryName


@dataclass(frozen=True)
class GithubFixtureRequest:
    listed: dict[str, object]
    body: str


@dataclass(frozen=True)
class GithubFixture:
    repo: RepositoryName
    requests: dict[str, GithubFixtureRequest]
    diff: str = ""
    call_file: ClassVar[str] = "calls.jsonl"

    def write(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        program = directory / "gh"
        source = f"#!{sys.executable}\n" + Path(__file__).read_text().partition("\n")[2]
        if program.exists():
            if program.read_text() != source:
                raise ValueError(f"The fixture refuses another executable at {program}")
        else:
            program.write_text(source)
            program.chmod(0o755)
        for number, request in self.requests.items():
            response = self.address(directory, f"repos/{self.repo.to_wire()}/pulls/{number}")
            response.parent.mkdir(parents=True, exist_ok=True)
            response.write_text(json.dumps(dict(request.listed, body=request.body)))
            comparison = self.address(directory, f"repos/{self.repo.to_wire()}/compare/"
                f"{request.listed['base']}...{request.listed['head']}")
            comparison.parent.mkdir(parents=True, exist_ok=True)
            comparison.write_text(self.diff)

    @staticmethod
    def address(directory: Path, endpoint: str) -> Path:
        path = directory.joinpath(endpoint)
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("A fixture response cannot leave its directory")
        return path

    @classmethod
    def read(cls, directory: Path, repo: str, number: str) -> dict[str, object]:
        return json.loads(cls.address(directory, f"repos/{repo}/pulls/{number}").read_text())

    @classmethod
    def calls(cls, directory: Path) -> tuple[list[str], ...]:
        return tuple(json.loads(line) for line in directory.joinpath(cls.call_file).read_text().splitlines())


def main():
    directory = Path(sys.argv[0]).parent
    arguments = sys.argv[1:]
    with directory.joinpath(GithubFixture.call_file).open("a") as calls:
        calls.write(json.dumps(arguments) + "\n")
    if arguments[:2] == ["pr", "view"]:
        repo = arguments[arguments.index("-R") + 1]
        request = GithubFixture.read(directory, repo, arguments[2])
        field = arguments[arguments.index("--json") + 1]
        assert arguments[arguments.index("--jq") + 1] == "." + field
        print(request[field])
    elif arguments[:2] == ["api", "graphql"]:
        query = arguments[arguments.index("-f") + 1]
        assert query.startswith("query=")
        repository = re.search(r'repository\(owner: "([^"]+)", name: "([^"]+)"\)', query)
        assert repository is not None, "The query must select one repository"
        repo = "/".join(repository.groups())
        selections = re.findall(r"p(\d+): pullRequest\(number: (\d+)\)", query)
        assert selections, "Only explicit recorded pull-request selections are supported"
        assert all(alias == number for alias, number in selections)
        responses = {f"p{number}": GithubFixture.read(directory, repo, number)
                     for _, number in selections}
        for response in responses.values():
            del response["body"]
        print(json.dumps(responses))
    elif arguments[:3] == ["api", "--method", "POST"]:
        repo, separator, head = arguments[3].partition("/statuses/")
        assert separator and head and GithubFixture.address(directory, repo).is_dir()
        fields = dict(arguments[index + 1].split("=", 1)
                      for index, argument in enumerate(arguments) if argument == "-f")
        assert fields["state"] in ("success", "failure")
        print(json.dumps(fields))
    elif arguments[:1] == ["api"]:
        assert arguments[arguments.index("-H") + 1] == "Accept: application/vnd.github.diff"
        print(GithubFixture.address(directory, arguments[1]).read_text())
    else:
        raise AssertionError(f"Unexpected gh invocation: {arguments!r}")


if __name__ == "__main__":
    main()
