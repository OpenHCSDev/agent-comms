"""IRC model: channels, DMs, presence, history views."""

from agent_comms.channel_targets import CommandTarget
import pytest

from agent_comms.registration import Registration
from agent_comms.channels import AllChannel
from agent_comms.comms import wire
from agent_comms.errors import UnknownChannelError, UnregisteredThreadError
from agent_comms.messages import Message, InfoKind
from bus_history import read_history


@pytest.fixture
def chat(comms):
    """Only ci/docs exist; PR111 is owned by this actual process."""
    import os
    from agent_comms.child_process import ProcessIdentity
    from conftest import declare_test_bus

    return declare_test_bus(comms, channels=("ci", "docs"), threads=(
        comms.registry.new_thread('PR111', frozenset({'ci'}), '/tmp/wt1', process_identity=ProcessIdentity.capture(os.getpid())),
        comms.registry.new_thread('fixer', frozenset({'ci'}), '/tmp/wt1', parent='PR111', task='fix auth'),
        comms.registry.new_thread('PR112', frozenset({'docs'}), '/tmp/wt2'),
    ))


class TestChannelTargets:
    def test_message_to_channel_is_valid(self):
        message = Message(sender="a", target="#ci", body="x", type=InfoKind)
        assert message.target == "#ci"

    def test_message_to_global_channel_is_valid(self):
        message = Message(
            sender="a", target=AllChannel.declared_name, body="x", type=InfoKind
        )
        assert message.target == "#all"

    def test_rejects_bad_channel_characters(self):
        with pytest.raises(ValueError):
            Message(sender="a", target="#bad channel!", body="x", type=InfoKind)

    def test_rejects_empty_tag_channel(self):
        with pytest.raises(ValueError):
            Message(sender="a", target="#", body="x", type=InfoKind)

    def test_rejects_mixed_case_channel(self):
        with pytest.raises(ValueError):
            Message(sender="a", target="#CI", body="x", type=InfoKind)

    def test_self_send_to_channel_allowed(self):
        # Talking in a channel you're in is normal.
        Message(sender="a", target="#all", body="x", type=InfoKind)

    def test_self_dm_still_rejected(self):
        with pytest.raises(Exception):
            Message(sender="a", target="a", body="x", type=InfoKind)


class TestChannelDelivery:
    def test_undeclared_channel_is_refused_without_creation(self, chat):
        with pytest.raises(UnknownChannelError):
            chat.messaging.send("PR111", "#undeclared", "must not create a channel")
        assert "#undeclared" not in chat.channels.channels()
        assert chat.bus.pending_count("fixer") == 0

    def test_tag_channel_reaches_tagged_threads_only(self, chat):
        chat.messaging.send("PR111", "#ci", "ci flaked again").message.message_id
        assert chat.bus.pending_count("fixer") == 1  # carries tag 'ci'
        assert chat.bus.pending_count("PR112") == 0  # carries tag 'docs'
        assert chat.bus.pending_count("PR111") == 0  # sender excluded

    def test_global_channel_reaches_everyone(self, chat):
        chat.messaging.send("PR111", "#all", "main is green").message.message_id
        assert chat.bus.pending_count("fixer") == 1
        assert chat.bus.pending_count("PR112") == 1

    def test_dm_untagged_by_channels(self, chat):
        chat.messaging.send("PR111", "PR112", "dm").message.message_id
        assert chat.bus.pending_count("PR112") == 1
        assert chat.bus.pending_count("fixer") == 0

    def test_channel_send_fail_closed_unknown_dm(self, chat):
        with pytest.raises(UnregisteredThreadError):
            chat.messaging.send("PR111", "ghost", "hi").message.message_id

    def test_untagged_thread_does_not_receive_tag_channel(self, chat):
        # A thread with no tags receives only DMs and the global channel.
        chat.registry.declare(chat.registry.new_thread(name='loner', tags=frozenset(), worktree='/wt'))
        chat.messaging.send("PR111", "#ci", "only tagged see this").message.message_id
        assert chat.bus.pending_count("loner") == 0


class TestHistory:
    def test_dm_history_is_full_conversation(self, chat):
        chat.messaging.send("PR111", "fixer", "hello").message.message_id
        chat.messaging.send("fixer", "PR111", "hi back").message.message_id
        chat.messaging.send("PR111", "PR112", "unrelated").message.message_id
        history = [m.body for m in list(chat.views.history_page(CommandTarget.named("fixer"), viewer="PR111").messages)]
        assert history == ["hello", "hi back"]

    def test_dm_history_of_undeclared_peer_is_empty_and_does_not_register_it(self, chat):
        assert chat.views.history_page(CommandTarget.named("ghost"), viewer="PR111").messages == ()
        assert "ghost" not in chat.registry

    def test_channel_history_returns_all(self, chat):
        chat.messaging.send("PR111", "#ci", "one").message.message_id
        chat.messaging.send("fixer", "#ci", "two").message.message_id
        chat.messaging.send("PR111", "#all", "three").message.message_id
        assert [m.body for m in read_history(chat, "#ci")] == ["one", "two"]

    def test_global_channel_history(self, chat):
        chat.messaging.send("PR111", "#all", "hello").message.message_id
        assert [m.body for m in read_history(chat, "#all")] == ["hello"]
        with pytest.raises(ValueError):
            read_history(chat, "broadcast")

    def test_channel_history_rejects_dm_target(self, chat):
        with pytest.raises(ValueError):
            read_history(chat, "fixer")


class TestChannelsDerived:
    def test_channels_is_global_plus_tags(self, chat):
        assert chat.channels.channels() == ["#any", "#none", "#all", "#ci", "#docs"]

    def test_channels_empty_wire(self, tmp_path):
        comms = wire(tmp_path / "fresh")
        assert comms.channels.channels() == ["#any", "#none", "#all"]


class TestPresence:
    def test_register_sets_last_seen(self, tmp_path):
        registry = Registration(tmp_path / "r.json")
        registry.register(registry.new_thread(name='a', tags=frozenset(), worktree='/wt'))
        assert registry.last_seen("a") > 0

    def test_heartbeat_updates_last_seen(self, tmp_path):
        registry = Registration(tmp_path / "r.json")
        registry.register(registry.new_thread(name='a', tags=frozenset(), worktree='/wt'))
        before = registry.last_seen("a")
        import time

        time.sleep(0.01)
        registry.heartbeat("a")
        assert registry.last_seen("a") >= before

    def test_last_seen_fail_closed(self, tmp_path):
        registry = Registration(tmp_path / "r.json")
        with pytest.raises(UnregisteredThreadError):
            registry.last_seen("ghost")

    def test_last_seen_persists(self, tmp_path):
        path = tmp_path / "r.json"
        registry = Registration(path)
        registry.register(registry.new_thread(name='a', tags=frozenset(), worktree='/wt'))
        seen = registry.last_seen("a")
        assert Registration(path).last_seen("a") == seen

    def test_who_shape(self, chat):
        rows = {row.thread.name: row.to_wire() for row in chat.views.who()}
        assert rows["PR111"]["status"] == "running"
        assert rows["PR111"]["tags"] == ["ci"]
        assert rows["PR111"]["last_seen"] > 0
        assert rows["PR112"]["tags"] == ["docs"]


class TestCurrentThreadEnvironment:
    def test_agent_comms_thread_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "me")
        monkeypatch.chdir(tmp_path)
        from agent_comms.threads import current_thread

        assert current_thread(wire(tmp_path).registry).name == "me"
