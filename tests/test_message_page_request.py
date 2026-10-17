"""Display owners preserve membership and progress through indexed paging."""
from agent_comms.storage_rules import StorageRule

from dataclasses import dataclass
import pytest

from agent_comms.comms import wire
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import MessageDisplayScope
from agent_comms.threads import Thread


@dataclass(frozen=True)
class ReviewScope(MessageDisplayScope):
    prefix: str
    index_targets = None

    def includes(self, message):
        return message.body.startswith(self.prefix)


@pytest.mark.parametrize("direction", [{"after": 0}, {"before": 10}])
def test_new_scope_preserves_indexed_progress_and_edges(tmp_path, direction):
    comms = wire(tmp_path)
    for name in ("sender", "recipient"):
        comms.registry.declare(Thread.create(name, frozenset({"review"}), str(tmp_path), storage_rule=StorageRule()))
    messages = [
        comms.messaging.send_message("sender", "recipient", body)
        for body in ("noise", "review first", "review " + "x" * 3000,
                     "noise", "review last")
    ]
    # No consumer edit/roster entry: one declared scope is sufficient.
    request = MessagePageRequest.capture(ReviewScope("review"), max_bytes=1, **direction)
    indexed = request.read(comms.bus.log)
    assert len(indexed.messages) == 1
    assert indexed.messages[0] == messages[1 if "after" in direction else 4]
    assert indexed.has_newer == ("after" in direction)
    assert indexed.has_older == ("before" in direction)
    cursor = indexed.newest_seq if "after" in direction else indexed.oldest_seq
    continuation = {"after" if "after" in direction else "before": cursor}
    next_page = MessagePageRequest.capture(
        request.scope, max_bytes=1, **continuation
    ).read(comms.bus.log)
    assert next_page.messages == (messages[2],)
    assert next_page.has_older and next_page.has_newer
