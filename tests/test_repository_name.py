"""Repository identities cross the wire and SQL once, without case-sensitive aliases."""
from dataclasses import replace

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


def test_identity_uses_owned_components_not_rendered_text(monkeypatch):
    first = RepositoryName("org", "first")
    second = RepositoryName("org", "second")
    monkeypatch.setattr(RepositoryName, "to_wire", lambda self: "display")
    assert first != second
    assert first == RepositoryName("ORG", "FIRST")


def test_repository_config_json_and_saved_todo_have_the_same_identity(commander):
    comms, _ = commander
    todo = create(commander, "P1")
    repo = Repo.model_validate_json('{"checkout":"/unused", "github":"OPENHCSDEV/AGENT-COMMS"}')
    assert repo.holds(todo)
    assert Repo.model_validate_json(repo.model_dump_json()) == repo
    assert comms.goals.todos.list(repo.github) == (todo,)
    restored = comms.goals.todos.get(todo.id)
    assert restored.repo == repo.github
    assert FieldCodec.decode(type(todo), FieldCodec.encode(todo)) == restored
    assert not Repo(checkout=repo.checkout, github=RepositoryName("another", "repo")).holds(todo)


def test_order_family_owns_which_kinds_a_merge_closes(commander):
    comms, _ = commander
    todo = create(commander, "P1")
    finding = replace(todo, order=UnorderedProblemOrder())
    assert finding.order.closed_by_merge(finding, (), comms.registry.storage_rule, PullRequests((finding,))) == ()
