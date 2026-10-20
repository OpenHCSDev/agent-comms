"""Repository identities cross the wire and SQL once, without case-sensitive aliases."""
from dataclasses import replace

import pytest

from agent_comms.commander.config import Repo
from agent_comms.field_codec import FieldCodec
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import PullRequests, UnorderedProblemOrder
from test_commander_problem_orders import create


def test_repository_identity_hash_and_existing_wire_form():
    first = RepositoryName("OpenHCSDev", "Agent-Comms")
    second = RepositoryName.from_wire("OPENHCSDEV/agent-comms")
    assert first == second
    assert len({first, second}) == 1
    assert FieldCodec.encode(first) == "openhcsdev/agent-comms"
    assert FieldCodec.decode(RepositoryName, FieldCodec.encode(first)) == first


@pytest.mark.parametrize("owner", ["ß" * 100, "İ"])
def test_repository_rejects_names_whose_canonical_wire_cannot_be_decoded(owner):
    with pytest.raises(ValueError):
        RepositoryName(owner, "repo")


