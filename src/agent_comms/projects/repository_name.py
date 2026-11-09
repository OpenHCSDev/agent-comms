"""A GitHub repository identity, with its existing owner/name wire form."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import ClassVar

from agent_comms.nominal.field_codec import MalformedRecord, WireValue

# A read that outlives the timeout is attempted again: one stalled call otherwise fails
# the run that made it, and in the gate that run is the one the merge queue waits on.
# A request naming its method is attempted once, because a timeout cannot say whether
# it was applied.
READ_ATTEMPTS = 2


READ_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True, eq=False)
class RepositoryName(WireValue):
    owner: str
    name: str
    _part: ClassVar[re.Pattern[str]] = re.compile(r"(?!\.{1,2}$)[\w.-]+\Z")

    def __post_init__(self) -> None:
        if len(self.to_wire()) > 200 or not all(self._part.fullmatch(part) for part in self.key):
            raise ValueError("Repo ID must have the exact owner/repo form.")

    @property
    def key(self) -> tuple[str, str]:
        return self.owner.casefold(), self.name.casefold()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RepositoryName):
            return NotImplemented
        return self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def named(self, name: str) -> bool:
        return name.casefold() in (self.to_wire(), self.name.casefold())

    def to_wire(self) -> str:
        return "/".join(self.key)

    @staticmethod
    def command(*arguments: str, input: str = "") -> subprocess.CompletedProcess[str]:
        """The GitHub process boundary shared by API reads and normal merge requests.

        A read that stalls past its timeout is attempted again; a request that names
        its method is attempted once and its timeout is the caller's answer.
        """
        attempts = 1 if "--method" in arguments else READ_ATTEMPTS
        attempt = 0
        while True:
            attempt += 1
            try:
                return subprocess.run(("gh", *arguments), input=input, capture_output=True,
                                      text=True, timeout=READ_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                if attempt >= attempts:
                    raise

    def api(self, *arguments: str, input: str = "") -> subprocess.CompletedProcess[str]:
        """GitHub's API protocol; callers judge its response."""
        return self.command("api", *arguments, input=input)

    def graphql(self, query: str, *arguments: str,
                **variables: str) -> subprocess.CompletedProcess[str]:
        """Send the complete request body on stdin, outside exec's argument-size limit."""
        return self.api("graphql", "--input", "-", *arguments,
                        input=json.dumps({"query": query, "variables": variables}))

    def rest(self, path: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        """``path`` under this repository's REST root."""
        return self.api(f"repos/{self.to_wire()}/{path}", *arguments)

    @classmethod
    def from_wire(cls, data: object) -> RepositoryName:
        if not isinstance(data, str):
            raise MalformedRecord("Repo ID must be an owner/repo string.")
        owner, _, name = data.partition("/")
        return cls(owner, name)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return {"type": "string", "description": "GitHub repository owner/name"}
