from pathlib import Path

from agent_comms.comms import client_wire
from agent_comms.practice_fixture import (EmptyInboxScenario, FixtureRequest,
                                          ReplacedTargetScenario, prepare)


def request(tmp_path: Path) -> FixtureRequest:
    return FixtureRequest(tmp_path / "root", tmp_path / "work", "reader", "sender",
                          "target", "replacement", "body")


def test_fixture_uses_real_registry_and_bus(tmp_path):
    fixture = request(tmp_path)
    prepare(fixture, EmptyInboxScenario)
    with client_wire(fixture.root) as comms:
        assert {"reader", "sender"} <= set(comms.registry.all_threads())
        sent = comms.messaging.send("sender", "reader", "hello")
        assert sent.message_id
        assert [message.body for message in comms.bus.inbox("reader").page.messages] == ["hello"]


def test_replacement_changes_real_incarnation(tmp_path):
    fixture = request(tmp_path)
    prepare(fixture, ReplacedTargetScenario)
    with client_wire(fixture.root) as comms:
        assert "target" in comms.registry.all_threads()
