"""Declared display scopes select membership before the sequence-page limit."""

from dataclasses import dataclass

import pytest

from agent_comms.channel_targets import CommandTarget
from agent_comms.comms import wire
from agent_comms.message_page import MessagePageRequest, PageBounds
from agent_comms.page_traversal import LiveCursor, NewerTraversal
from agent_comms.read_basis import MessageDisplayScope, NamedViewer
from bus_record_fixture import prepare_native_comms


@pytest.fixture
def review_comms(tmp_path):
    comms = prepare_native_comms(wire(tmp_path))
    for name in ("sender", "recipient"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'review'}), str(tmp_path)))
    return comms


@dataclass(frozen=True)
class ReviewScope(MessageDisplayScope):
    prefix: str
    index_targets = frozenset()

    @property
    def index_predicate(self):
        return "substr(body,1,length(?))=?", (self.prefix, self.prefix)

    def includes(self, message):
        return message.body.startswith(self.prefix)


@pytest.mark.parametrize("traversal,first,has_older,has_newer", [
    (NewerTraversal(), 1, False, True), (LiveCursor(10).older(), 4, True, False),
])
def test_new_scope_preserves_sequence_progress_and_edges(
    review_comms, traversal, first, has_older, has_newer,
):
    comms = review_comms
    messages = [
        comms.messaging.send("sender", "recipient", body).message
        for body in ("noise", "review first", "review " + "x" * 3000,
                     "noise", "review last")
    ]
    # No consumer edit/roster entry: one declared scope is sufficient.
    request = MessagePageRequest(ReviewScope("review"), PageBounds(traversal, limit=1))
    indexed = request.read_opened(comms.bus.bus)
    assert len(indexed.messages) == 1
    assert indexed.messages[0] == messages[first]
    assert indexed.has_newer == has_newer
    assert indexed.has_older == has_older
    continuation = traversal.continuation(indexed)
    assert continuation is not None
    next_page = MessagePageRequest(
        request.scope, PageBounds(continuation, limit=1)
    ).read_opened(comms.bus.bus)
    assert next_page.messages == (messages[2],)
    assert next_page.has_older and next_page.has_newer


@pytest.mark.parametrize("conversation", ["dm", "channel"])
def test_dm_and_channel_pages_by_default_and_explicit_bounds(review_comms, conversation):
    comms = review_comms
    target = "recipient" if conversation == "dm" else "#review"
    sent = [comms.messaging.send("sender", target, f"{conversation} {n}").message for n in range(4)]

    def page(bounds=PageBounds()):
        if conversation == "dm":
            return comms.views.history_page(CommandTarget.named("recipient"), bounds, viewer=NamedViewer("sender"))
        return comms.views.history_page(CommandTarget.named("#review"), bounds)

    everything = page()
    assert everything.messages == tuple(sent) and not everything.has_older
    newest = page(PageBounds(limit=2))
    assert newest.messages == tuple(sent[2:]) and newest.has_older and not newest.has_newer
    older = page(PageBounds(LiveCursor(newest.oldest_seq).older(), 2))
    assert older.messages == tuple(sent[:2]) and older.has_newer and not older.has_older
    one = page(PageBounds(LiveCursor(sent[0].seq).newer(), limit=1))
    assert one.messages == (sent[1],) and one.has_older and one.has_newer
