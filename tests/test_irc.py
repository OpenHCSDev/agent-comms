"""IRC model: channels, DMs, presence, history views."""

import pytest

from agent_comms import (
    GLOBAL_CHANNEL,
    Message,
    MessageType,
    Thread,
    ThreadRegistry,
    UnregisteredThreadError,
    wire,
)


@pytest.fixture
def chat(wired):
    """Re-tag: PR111 and fixer carry 'ci', PR112 carries 'docs'."""
    comms = wired
    pr111 = comms.registry.require("PR111")
    fixer = comms.registry.require("fixer")
    comms.register(
        Thread(name="PR111", tags=frozenset({"ci"}), worktree=pr111.worktree, pid=pr111.pid)
    )
    comms.register(
        Thread(
            name="fixer",
            tags=frozenset({"ci"}),
            worktree=fixer.worktree,
            parent="PR111",
            task=fixer.task,
            pid=fixer.pid,
        )
    )
    comms.register(Thread(name="PR112", tags=frozenset({"docs"}), worktree="/tmp/wt2", pid=300))
    return comms


class TestChannelTargets:
    def test_message_to_channel_is_valid(self):
        message = Message(sender="a", target="#ci", body="x", type=MessageType.INFO)
        assert message.target == "#ci"

    def test_message_to_global_channel_is_valid(self):
        message = Message(sender="a", target=GLOBAL_CHANNEL, body="x", type=MessageType.INFO)
        assert message.target == "#all"

    def test_rejects_bad_channel_characters(self):
        with pytest.raises(ValueError, match="Channel"):
            Message(sender="a", target="#bad channel!", body="x", type=MessageType.INFO)

    def test_rejects_empty_tag_channel(self):
        with pytest.raises(ValueError, match="Channel"):
            Message(sender="a", target="#", body="x", type=MessageType.INFO)

    def test_rejects_mixed_case_channel(self):
        with pytest.raises(ValueError, match="Channel"):
            Message(sender="a", target="#CI", body="x", type=MessageType.INFO)

    def test_self_send_to_channel_allowed(self):
        # Talking in a channel you're in is normal.
        Message(sender="a", target="#all", body="x", type=MessageType.INFO)

    def test_self_dm_still_rejected(self):
        with pytest.raises(Exception, match="itself"):
            Message(sender="a", target="a", body="x", type=MessageType.INFO)


class TestChannelDelivery:
    def test_tag_channel_reaches_tagged_threads_only(self, chat):
        chat.send("PR111", "#ci", "ci flaked again")
        assert chat.pending_count("fixer") == 1  # carries tag 'ci'
        assert chat.pending_count("PR112") == 0  # carries tag 'docs'
        assert chat.pending_count("PR111") == 0  # sender excluded

    def test_global_channel_reaches_everyone(self, chat):
        chat.broadcast("PR111", "main is green")
        assert chat.pending_count("fixer") == 1
        assert chat.pending_count("PR112") == 1

    def test_broadcast_alias_equals_global(self, chat):
        chat.send("PR111", "#all", "hello")
        assert chat.pending_count("fixer") == 1

    def test_dm_untagged_by_channels(self, chat):
        chat.send("PR111", "PR112", "dm")
        assert chat.pending_count("PR112") == 1
        assert chat.pending_count("fixer") == 0

    def test_channel_send_fail_closed_unknown_dm(self, chat):
        with pytest.raises(UnregisteredThreadError, match="Target"):
            chat.send("PR111", "ghost", "hi")

    def test_untagged_thread_does_not_receive_tag_channel(self, chat):
        # A thread with no tags receives only DMs and the global channel.
        chat.register(Thread(name="loner", tags=frozenset(), worktree="/wt"))
        chat.send("PR111", "#ci", "only tagged see this")
        assert chat.pending_count("loner") == 0


class TestHistory:
    def test_dm_history_is_full_conversation(self, chat):
        chat.send("PR111", "fixer", "hello")
        chat.send("fixer", "PR111", "hi back")
        chat.send("PR111", "PR112", "unrelated")
        history = [m.body for m in chat.dm_history("PR111", "fixer")]
        assert history == ["hello", "hi back"]

    def test_dm_history_requires_registered_threads(self, chat):
        with pytest.raises(UnregisteredThreadError):
            chat.dm_history("PR111", "ghost")

    def test_channel_history_returns_all(self, chat):
        chat.send("PR111", "#ci", "one")
        chat.send("fixer", "#ci", "two")
        chat.send("PR111", "#all", "three")
        assert [m.body for m in chat.channel_history("#ci")] == ["one", "two"]

    def test_channel_history_accepts_broadcast_alias(self, chat):
        chat.broadcast("PR111", "hello")
        assert [m.body for m in chat.channel_history("broadcast")] == ["hello"]
        assert [m.body for m in chat.channel_history("#all")] == ["hello"]

    def test_channel_history_rejects_dm_target(self, chat):
        with pytest.raises(ValueError, match="not a channel"):
            chat.channel_history("fixer")


class TestChannelsDerived:
    def test_channels_is_global_plus_tags(self, chat):
        assert chat.channels() == ["#all", "#ci", "#docs"]

    def test_channels_empty_wire(self, tmp_path):
        comms = wire(tmp_path / "fresh")
        assert comms.channels() == ["#all"]


class TestPresence:
    def test_register_sets_last_seen(self, tmp_path):
        registry = ThreadRegistry(tmp_path / "r.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        assert registry.last_seen("a") > 0

    def test_heartbeat_updates_last_seen(self, tmp_path):
        registry = ThreadRegistry(tmp_path / "r.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        before = registry.last_seen("a")
        import time

        time.sleep(0.01)
        registry.heartbeat("a")
        assert registry.last_seen("a") >= before

    def test_last_seen_fail_closed(self, tmp_path):
        registry = ThreadRegistry(tmp_path / "r.json")
        with pytest.raises(UnregisteredThreadError):
            registry.last_seen("ghost")

    def test_last_seen_persists(self, tmp_path):
        path = tmp_path / "r.json"
        registry = ThreadRegistry(path)
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        seen = registry.last_seen("a")
        assert ThreadRegistry(path).last_seen("a") == seen

    def test_who_shape(self, chat):
        rows = {row["name"]: row for row in chat.who()}
        assert rows["PR111"]["status"] == "running"
        assert rows["PR111"]["tags"] == ["ci"]
        assert rows["PR111"]["last_seen"] > 0
        assert rows["PR112"]["tags"] == ["docs"]


class TestCurrentThreadEnvFallback:
    def test_agent_comms_thread_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PI_AGENT_ID", raising=False)
        monkeypatch.setenv("AGENT_COMMS_THREAD", "me")
        monkeypatch.chdir(tmp_path)
        from agent_comms import current_thread

        assert current_thread().name == "me"

    def test_pi_agent_id_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PI_AGENT_ID", "pi-thread")
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fallback")
        monkeypatch.chdir(tmp_path)
        from agent_comms import current_thread

        assert current_thread().name == "pi-thread"
