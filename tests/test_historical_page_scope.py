"""Source-owned history predicates and sound use of the existing offset index."""
from agent_comms.storage_rules import StorageRule

import json
from dataclasses import replace

import pytest

from agent_comms.comms import Comms
from agent_comms.historical_views import HistoryCursor
from agent_comms.mentions import ThreadMention
from agent_comms.messages import Message, InfoKind
from agent_comms.threads import Thread


@pytest.fixture
def history(tmp_path):
    old = Comms(tmp_path / "old")
    old.registry.declare(Thread.create("alice-old", frozenset({"api"}), str(old.root), created_at=10, storage_rule=StorageRule()))
    for created, name in enumerate(("bob", "carol"), start=11):
        old.registry.declare(Thread.create(name, frozenset({"ops"}), str(old.root), created_at=created, storage_rule=StorageRule()))
    old.registry.rename("alice-old", "alice")
    old.messaging.initialize_private_initial_protocol()
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
        raw = (json.dumps(message.to_wire()) + "\n").encode()
        rows.append((message, raw))
    old.bus.log.path.write_bytes(b"".join(raw for _, raw in rows))
    old.bus.log.path.chmod(0o600)
    marker = old.bus.log.read_metadata_unlocked(required=True)
    marker.last_seq = marker.claimed_after_seq = rows[-1][0].seq
    old.bus.log.write_metadata_unlocked(marker)
    live = Comms(tmp_path / "live")
    # Deliberately different membership: source scope must not use live tags.
    live.registry.declare(Thread.create("alice", frozenset({"ops"}), str(live.root), created_at=500, storage_rule=StorageRule()))
    live.registry.declare(Thread.create("alice-old", frozenset({"api"}), str(live.root), created_at=501, storage_rule=StorageRule()))
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
        return lambda **kwargs: live.views.dm_history_page(name, "carol", **kwargs)
    if kind == "any-mode":
        live.channels.set_channel_any_mode("#api", True)
        return lambda **kwargs: live.views.channel_display_page("#api", **kwargs)
    target = {
        "channel": "#api",
        "any": "#any",
        "all": "#all",
        "none": "#none",
    }[kind]
    return lambda **kwargs: live.views.channel_history_page(target, **kwargs)


@pytest.mark.parametrize(
    "kind", ["channel", "any-mode", "any", "all", "none", "dm", "dm-alias"]
)
def test_complete_forward_and_reverse_traversal_preserves_membership(history, kind):
    live, source, rows = history
    read = reader(live, kind)
    expected = [message.to_wire() for message, _ in rows if selected(kind, message)]
    page = read(limit=2, max_bytes=600)
    backward = [message.to_wire() for message in page.messages]
    for _ in range(len(rows) + 1):
        if not page.has_older:
            break
        page = read(before=page.oldest_cursor, limit=2, max_bytes=600)
        backward[0:0] = [message.to_wire() for message in page.messages]
    else:
        raise AssertionError("reverse history cursor did not terminate")
    assert backward == expected
    cursor = HistoryCursor(source.key, 0)
    forward = []
    for _ in range(len(rows) + 1):
        page = read(after=cursor, limit=2, max_bytes=600)
        forward.extend(message.to_wire() for message in page.messages)
        if not page.has_newer:
            break
        cursor = page.newest_cursor
    else:
        raise AssertionError("forward history cursor did not terminate")
    assert forward == expected
