"""Source-owned history predicates and sound use of the existing offset index."""

import json
from dataclasses import replace

import pytest

from agent_comms.comms import Comms
from agent_comms.historical_views import ChannelDisplayHistory, HistoryCursor
from agent_comms.mentions import ThreadMention
from agent_comms.messages import Message, MessageType
from agent_comms.read_basis import ChannelDisplayScope
from agent_comms.threads import Thread


@pytest.fixture
def history(tmp_path):
    old = Comms(tmp_path / "old")
    old.registry.declare(Thread("alice-old", frozenset({"api"}), str(old.root), created_at=10))
    for created, name in enumerate(("bob", "carol"), start=11):
        old.registry.declare(Thread(name, frozenset({"ops"}), str(old.root), created_at=created))
    old.registry.rename("alice-old", "alice")
    old.messaging.initialize_private_initial_protocol()
    rows = []
    for seq in range(1, 241):
        message = Message(
            "bob", "#noise", f"unrelated {seq}", MessageType.INFO, seq=seq, timestamp=100 + seq
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
    marker.last_seq = marker.admission_after_seq = rows[-1][0].seq
    old.bus.log.write_metadata_unlocked(marker)
    live = Comms(tmp_path / "live")
    # Deliberately different membership: source scope must not use live tags.
    live.registry.declare(Thread("alice", frozenset({"ops"}), str(live.root), created_at=500))
    live.registry.declare(Thread("alice-old", frozenset({"api"}), str(live.root), created_at=501))
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
@pytest.mark.parametrize(
    "before,after", [(None, None), (180, None), (1, None), (None, 0), (None, 190)]
)
@pytest.mark.parametrize("limit,budget", [(2, 1), (8, 2000)])
def test_cold_and_warm_historical_pages_equal_full_scan(
    history, kind, before, after, limit, budget
):
    live, source, rows = history
    read = reader(live, kind)
    expected = live.bus._collect_history_page(
        ((message, len(raw)) for message, raw in rows),
        lambda message: selected(kind, message),
        before=before,
        after=after,
        limit=limit,
        max_bytes=budget,
    )
    for _ in range(2):  # cold validation, then the same warm offsets
        page = read(
            before=HistoryCursor(source.key, before) if before is not None else None,
            after=HistoryCursor(source.key, after) if after is not None else None,
            limit=limit,
            max_bytes=budget,
        )
        assert [message.to_wire() for message in page.messages] == [
            message.to_wire() for message in expected.messages
        ]
        assert [message.view_key for message in page.messages] == [
            (source.key, message.seq) for message in expected.messages
        ]
        # Historical traversal consumes empty source pages; its terminal empty
        # page has no further edges even if the raw collector saw newer rows.
        assert page.has_older == (expected.has_older if expected.messages else False)
        assert page.has_newer == (expected.has_newer if expected.messages else False)
        assert page.oldest_cursor == (
            HistoryCursor(source.key, expected.oldest_seq) if expected.messages else None
        )
        assert page.newest_cursor == (
            HistoryCursor(source.key, expected.newest_seq) if expected.messages else None
        )


def test_sparse_page_decodes_only_index_candidates_after_cold_validation(history, monkeypatch):
    live, source, rows = history
    channel = live.channels.catalog.read().views(live.registry.snapshot().threads)["#api"]
    view = ChannelDisplayHistory(channel)
    decoded = 0
    captured = 0
    original_decode = Message.from_wire
    original_capture = ChannelDisplayScope.capture

    def decode(cls, raw):
        nonlocal decoded
        decoded += 1
        return original_decode(raw)

    def capture(cls, channel, snapshot, **kwargs):
        nonlocal captured
        captured += 1
        return original_capture(channel, snapshot, **kwargs)

    monkeypatch.setattr(Message, "from_wire", classmethod(decode))
    monkeypatch.setattr(ChannelDisplayScope, "capture", classmethod(capture))
    cold = live.bus.historical_page(view, limit=2)
    cold_decoded = decoded
    decoded = captured = 0
    warm = live.bus.historical_page(view, limit=2)
    assert warm == cold
    assert [message.seq for message in warm.messages] == [150, 200]
    assert decoded == 3  # two returned rows and the earlier-match boundary
    assert cold_decoded == len(rows) + decoded  # canonical cold validation remains
    assert captured == 1
    narrowed_decodes = decoded
    decoded = 0
    with monkeypatch.context() as patch:
        patch.setattr(ChannelDisplayScope, "index_targets", property(lambda _scope: None))
        unfiltered = live.bus.historical_page(view, limit=2)
    assert unfiltered == warm
    assert decoded == 141  # same predicate/page without the target prefilter
    assert narrowed_decodes == 3


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
