"""Display owners preserve membership and progress through indexed paging."""

from dataclasses import dataclass
import pytest

from agent_comms.comms import wire
from agent_comms.channel_targets import CommandTarget
from agent_comms.message_page import MessagePageRequest, PageBounds
from agent_comms.page_traversal import NewerTraversal, OlderTraversal
from agent_comms.read_basis import MessageDisplayScope
from agent_comms.threads import Thread


@dataclass(frozen=True)
class ReviewScope(MessageDisplayScope):
    prefix: str
    index_targets = None

    @property
    def index_predicate(self):
        return "1", ()

    def includes(self, message):
        return message.body.startswith(self.prefix)


@pytest.mark.parametrize("traversal,first,has_older,has_newer", [
    (NewerTraversal(), 1, False, True), (OlderTraversal(10), 4, True, False),
])
def test_new_scope_preserves_indexed_progress_and_edges(
    tmp_path, traversal, first, has_older, has_newer,
):
    comms = wire(tmp_path)
    for name in ("sender", "recipient"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'review'}), str(tmp_path)))
    messages = [
        comms.messaging.send("sender", "recipient", body).message
        for body in ("noise", "review first", "review " + "x" * 3000,
                     "noise", "review last")
    ]
    # No consumer edit/roster entry: one declared scope is sufficient.
    request = MessagePageRequest(ReviewScope("review"), PageBounds(traversal, max_bytes=1))
    indexed = comms.bus.log.read_page(request)
    assert len(indexed.messages) == 1
    assert indexed.messages[0] == messages[first]
    assert indexed.has_newer == has_newer
    assert indexed.has_older == has_older
    continuation = type(traversal)(indexed.messages[0].seq)
    next_page = comms.bus.log.read_page(MessagePageRequest(
        request.scope, PageBounds(continuation, max_bytes=1)
    ))
    assert next_page.messages == (messages[2],)
    assert next_page.has_older and next_page.has_newer


@pytest.mark.parametrize("conversation", ["dm", "channel"])
def test_dm_and_channel_pages_by_default_and_explicit_bounds(tmp_path, conversation):
    comms = wire(tmp_path)
    for name in ("sender", "recipient"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'review'}), str(tmp_path)))
    target = "recipient" if conversation == "dm" else "#review"
    sent = [comms.messaging.send("sender", target, f"{conversation} {n}").message for n in range(4)]

    def page(bounds=PageBounds()):
        if conversation == "dm":
            return comms.views.history_page(CommandTarget.named("recipient"), bounds, viewer="sender")
        return comms.views.history_page(CommandTarget.named("#review"), bounds)

    everything = page()
    assert everything.messages == tuple(sent) and not everything.has_older
    newest = page(PageBounds(limit=2))
    assert newest.messages == tuple(sent[2:]) and newest.has_older and not newest.has_newer
    older = page(PageBounds(OlderTraversal(newest.oldest_seq), 2))
    assert older.messages == tuple(sent[:2]) and older.has_newer and not older.has_older
    one = page(PageBounds(NewerTraversal(sent[0].seq), max_bytes=1))
    assert one.messages == (sent[1],) and one.has_older and one.has_newer
