"""IRC model: channels, DMs, presence, history views."""
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.errors import UnknownChannelError, UnregisteredThreadError
from agent_comms.messaging.channel_targets import CommandTarget, DeclaredTag
from agent_comms.messaging.channels import AllChannel, BuiltinChannelView
from agent_comms.messaging.messages import InfoKind, Message
from agent_comms.messaging.read_basis import NamedViewer
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_store import UncachedRegistry
from agent_comms.registry.thread_text import (
    ForkedParent,
    GivenThreadText,
)
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import RegistryFile
from bus_history import read_history
from conftest import installed_comms


@pytest.fixture
def chat(comms):
    """ci/docs join the declared tags; PR111 is owned by this actual process."""
    import os

    from agent_comms.execution.child_process import ProcessIdentity
    from conftest import declare_test_bus

    return declare_test_bus(comms, channels=("ci", "docs"), threads=(
        NativeThread('PR111', frozenset({'ci'}), Checkout(Path('/tmp/wt1')), process_identity=ProcessIdentity.capture(os.getpid())),
        NativeThread('fixer', frozenset({'ci'}), Checkout(Path('/tmp/wt1')), parent=ForkedParent('PR111'), task=GivenThreadText('fix auth')),
        NativeThread('PR112', frozenset({'docs'}), Checkout(Path('/tmp/wt2'))),
    ))


class TestChannelTargets:
    @pytest.mark.parametrize("target", ["#ci", AllChannel.address()])
    def test_a_sender_may_address_a_channel(self, target):
        assert Message(sender="a", target=target, body="x", type=InfoKind).target == target

    @pytest.mark.parametrize("target", ["#bad channel!", "#", "#CI"])
    def test_rejects_malformed_channel_names(self, target):
        with pytest.raises(ValueError):
            Message(sender="a", target=target, body="x", type=InfoKind)


class TestChannelDelivery:
    def test_undeclared_channel_is_refused_without_creation(self, chat):
        with pytest.raises(UnknownChannelError):
            chat.messaging.send("PR111", "#undeclared", "must not create a channel")
        assert "#undeclared" not in chat.channels.channels()
        assert chat.bus.pending_count("fixer") == 0

    @pytest.mark.parametrize(("target", "reached"), [
        ("#ci", {"fixer"}),  # a tag channel reaches tagged threads only, never the sender
        ("#all", {"fixer", "PR112", "loner"}),  # the global channel reaches everyone
        ("PR112", {"PR112"}),  # a DM ignores tags
    ])
    def test_delivery_reaches_exactly_the_target_audience(self, chat, target, reached):
        chat.registry.declare(NativeThread(name='loner', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        chat.messaging.send("PR111", target, "x")
        assert {name for name in ("PR111", "fixer", "PR112", "loner")
                if chat.bus.pending_count(name)} == reached

    def test_channel_send_fail_closed_unknown_dm(self, chat):
        with pytest.raises(UnregisteredThreadError):
            chat.messaging.send("PR111", "ghost", "hi")


class TestHistory:
    def test_dm_history_is_full_conversation(self, chat):
        chat.messaging.send("PR111", "fixer", "hello")
        chat.messaging.send("fixer", "PR111", "hi back")
        chat.messaging.send("PR111", "PR112", "unrelated")
        history = [m.body for m in list(chat.views.history_page(CommandTarget.named("fixer"), viewer=NamedViewer("PR111")).messages)]
        assert history == ["hello", "hi back"]

    def test_dm_history_of_undeclared_peer_is_empty_and_does_not_register_it(self, chat):
        assert chat.views.history_page(CommandTarget.named("ghost"), viewer=NamedViewer("PR111")).messages == ()
        assert "ghost" not in chat.registry

    def test_channel_history_returns_exactly_that_channel(self, chat):
        chat.messaging.send("PR111", "#ci", "one")
        chat.messaging.send("fixer", "#ci", "two")
        chat.messaging.send("PR111", "#all", "three")
        assert [m.body for m in read_history(chat, "#ci")] == ["one", "two"]
        assert [m.body for m in read_history(chat, "#all")] == ["three"]
        with pytest.raises(ValueError):
            read_history(chat, "broadcast")

    def test_channel_history_rejects_dm_target(self, chat):
        with pytest.raises(ValueError):
            read_history(chat, "fixer")


class TestChannelsDerived:
    def test_channels_is_global_plus_tags(self, chat):
        declared = {member.channel() for member in DeclaredTag.members_with(DeclaredTag)}
        assert chat.channels.channels() == [*BuiltinChannelView.addresses(), *sorted(declared | {"#ci", "#docs"})]

    def test_channels_empty_wire(self, tmp_path):
        comms = installed_comms(tmp_path / "fresh")
        declared = sorted(member.channel() for member in DeclaredTag.members_with(DeclaredTag))
        assert comms.channels.channels() == [*BuiltinChannelView.addresses(), *declared]


class TestPresence:
    def test_heartbeat_updates_last_seen(self, tmp_path):
        registry = Registration(tmp_path / "r.json")
        registry.register(NativeThread(name='a', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        before = registry.last_seen("a")
        import time

        time.sleep(0.01)
        registry.heartbeat("a")
        assert registry.last_seen("a") > before

    def test_a_heartbeat_leaves_the_registry_file_alone(self, tmp_path):
        """P1270: being seen is presence, not a declaration change. The heartbeat moves only
        presence.json, so registry.json (5.7 MB on the live bus) is not rewritten for it, and
        a reader that opened the registry earlier still sees the new time."""
        path = RegistryFile.path_in(tmp_path)
        registry = Registration(path)
        registry.register(NativeThread(name='a', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        reader = Registration(path)
        before, stored = reader.last_seen("a"), path.read_bytes()
        stat = path.stat()
        import time

        time.sleep(0.01)
        registry.heartbeat("a")
        assert path.read_bytes() == stored and path.stat().st_mtime_ns == stat.st_mtime_ns
        assert reader.last_seen("a") > before
        assert reader.store.snapshot().seen_at("a") == reader.last_seen("a")

    def test_a_reader_holding_an_older_revision_still_finds_its_threads_presence(self, tmp_path):
        """#4190 send-back (f-ci): a reader captures the registry, a rename commits, then the
        reader lays presence over what it captured. Rows are never dropped, so the old
        revision's thread still has one, and the renamed thread reads under its new name."""
        path = RegistryFile.path_in(tmp_path)
        registry = Registration(path)
        registry.register(NativeThread(name='a', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        reader = Registration(path).store
        captured, = reader._copy_sources(reader)
        registry.rename("a", "b")
        reader.cache.entry = UncachedRegistry()  # decode the old bytes as a fresh process would
        assert reader._decode_revision(captured).document.seen_at("a") > 0
        assert Registration(path).last_seen("b") > 0

    def test_a_removed_thread_keeps_its_row_and_a_redeclared_name_gets_a_fresh_one(self, tmp_path):
        """f-ci's two cases: a plain removal (its own edit path) leaves a reader's earlier
        capture decodable, and declaring the old name again writes a new time over its row."""
        import time

        from agent_comms.registry.presence import PresenceStore

        path = RegistryFile.path_in(tmp_path)
        registry = Registration(path)
        registry.register(NativeThread(name='a', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        reader = Registration(path).store
        captured, = reader._copy_sources(reader)
        registry.remove("a")
        reader.cache.entry = UncachedRegistry()
        stale = reader._decode_revision(captured).document.seen_at("a")
        assert PresenceStore(tmp_path).read().seen["a"] == stale
        time.sleep(0.01)
        registry.register(NativeThread(name='a', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        assert Registration(path).last_seen("a") > stale

    def test_last_seen_fail_closed(self, tmp_path):
        registry = Registration(tmp_path / "r.json")
        with pytest.raises(UnregisteredThreadError):
            registry.last_seen("ghost")

    def test_last_seen_persists(self, tmp_path):
        path = tmp_path / "r.json"
        registry = Registration(path)
        registry.register(NativeThread(name='a', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        seen = registry.last_seen("a")
        assert seen > 0 and Registration(path).last_seen("a") == seen

    def test_who_shape(self, chat):
        rows = {row.thread.name: row.to_wire() for row in chat.views.who()}
        assert rows["PR111"]["status"] == "running"
        assert rows["PR111"]["tags"] == ["ci"]
        assert rows["PR111"]["last_seen"] > 0
        assert rows["PR112"]["tags"] == ["docs"]


class TestCurrentThreadEnvironment:
    def test_current_thread_decodes_its_launch_record(self, monkeypatch, tmp_path):
        from agent_comms.execution.launch_runtime import ThreadLaunch
        from agent_comms.registry.threads import current_thread

        monkeypatch.setenv(ThreadLaunch.variable("name"), "me")
        monkeypatch.setenv(ThreadLaunch.variable("worktree"), str(tmp_path))
        thread = current_thread(wire(tmp_path).registry)
        assert (thread.name, thread.worktree) == ("me", Checkout(tmp_path))
