"""Source-owned history predicates and sound sequence-indexed membership."""
from dataclasses import replace

import pytest

from agent_comms.bus import Bus, PublicRecord
from agent_comms.channel_targets import CommandTarget
from agent_comms.comms import Comms
from agent_comms.mentions import ThreadMention
from agent_comms.message_page import PageBounds
from agent_comms.messages import InfoKind, Message
from agent_comms.page_traversal import HistoryCursor
from agent_comms.read_basis import NamedViewer


@pytest.fixture
def history(tmp_path):
    old = Comms(tmp_path / "old")
    old.registry.declare(old.registry.new_thread('alice-old', frozenset({'api'}), str(old.root), created_at=10))
    for created, name in enumerate(("bob", "carol"), start=11):
        old.registry.declare(old.registry.new_thread(name, frozenset({'ops'}), str(old.root), created_at=created))
    old.registry.rename("alice-old", "alice")
    Bus.install(Bus.path_in(old.root))
    rows = []
    for seq in range(1, 241):
        message = Message(
            "bob", "#noise", f"unrelated {seq}", InfoKind, seq=seq, timestamp=100 + seq
        )
        if seq % 50 == 0:
            message = replace(message, target="#api")
        elif seq == 105:
            message = replace(message, sender="alice-old", target="carol")
        elif seq == 180:
            message = replace(message, sender="carol", target="alice-old")
        elif seq == 210:
            message = replace(
                message,
                sender="carol",
                target="#ops",
                body="@alice-old ping",
                mentions=(ThreadMention("alice-old", 0, 10),),
            )
        elif seq == 235:
            message = replace(message, target="#all")
        published = old.bus.bus.append(
            lambda seq, db: PublicRecord.from_message(replace(message, seq=seq))
        )
        rows.append(Message.from_message(published))
    live = Comms(tmp_path / "live")
    Bus.install(Bus.path_in(live.root))
    # Deliberately different membership: source scope must not use live tags.
    live.registry.declare(live.registry.new_thread('alice', frozenset({'ops'}), str(live.root), created_at=500))
    live.registry.declare(live.registry.new_thread('alice-old', frozenset({'api'}), str(live.root), created_at=501))
    live.registry.rename("alice-old", "carol")
    source = live.views.attach_history(old.root)
    return live, source, rows


def selected(kind, message):
    if kind == "any":
        return True
    if kind == "all":
        return message.target == "#all"
    if kind == "none":
        return message.target == "#none"
    if kind in ("dm", "dm-alias"):
        pair = {
            ("alice" if name == "alice-old" else name) for name in (message.sender, message.target)
        }
        return pair == {"alice", "carol"}
    if kind == "channel":
        return message.target == "#api"
    assert kind == "any-mode"
    return (
        message.target == "#api"
        or message.sender in {"alice", "alice-old"}
        or message.target in {"alice", "alice-old"}
        or any(mention.thread in {"alice", "alice-old"} for mention in message.mentions)
    )


def reader(live, kind):
    if kind in ("dm", "dm-alias"):
        name = "alice-old" if kind == "dm-alias" else "alice"
        return lambda bounds: live.views.history_page(CommandTarget.named("carol"), bounds, viewer=NamedViewer(name))
    if kind == "any-mode":
        live.channels.set_channel_any_mode("#api", True)
        return lambda bounds: live.views.channel_display_page("#api", bounds=bounds)
    target = {
        "channel": "#api",
        "any": "#any",
        "all": "#all",
        "none": "#none",
    }[kind]
    return lambda bounds: live.views.history_page(CommandTarget.named(target), bounds)


@pytest.mark.parametrize(
    "kind", ["channel", "any-mode", "any", "all", "none", "dm", "dm-alias"]
)
def test_complete_forward_and_reverse_traversal_preserves_membership(history, kind):
    live, source, rows = history
    read = reader(live, kind)
    expected = [message.to_wire() for message in rows if selected(kind, message)]
    page = read(PageBounds(limit=2))
    backward = [message.to_wire() for message in page.messages]
    for _ in range(len(rows) + 1):
        if not page.has_older:
            break
        page = read(PageBounds(page.oldest_cursor.older(), 2))
        backward[0:0] = [message.to_wire() for message in page.messages]
    else:
        raise AssertionError("reverse history cursor did not terminate")
    assert backward == expected
    cursor = HistoryCursor(source=source.key, sequence=0)
    forward = []
    for _ in range(len(rows) + 1):
        page = read(PageBounds(cursor.newer(), 2))
        forward.extend(message.to_wire() for message in page.messages)
        if not page.has_newer:
            break
        cursor = page.newest_cursor
    else:
        raise AssertionError("forward history cursor did not terminate")
    assert forward == expected
