"""Thread-list pending counts agree with current routing after a rename."""

from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread


def test_rename_alias_and_real_thread_named_broadcast_match_existing_scope(wired):
    wired.registry.declare(NativeThread(
        name="broadcast", tags=frozenset(), worktree=Checkout(wired.root / "broadcast")))
    wired.messaging.send("PR111", "broadcast", "direct message to the named thread")
    wired.messaging.send("fixer", "broadcast", "second sender")
    wired.messaging.send("PR111", "fixer", "old direct")
    wired.registry.rename("PR111", "renamed")

    def consistent():
        selected = wired.registry.all_threads()
        expected = {name: wired.bus.pending_count(name) for name in selected}
        assert {row.thread.name: row.pending.value for row in wired.views.list_threads()} == expected

    consistent()
    shown = wired.bus.inbox("broadcast", CommandTarget.named("PR111")).page.messages
    wired.bus.mark_read_through("broadcast", shown[-1].seq, target=CommandTarget.named("PR111"))
    consistent()


def test_the_thread_list_is_the_default_roster_not_every_archived_inbox(wired):
    """An archived thread has its own readers. Listing every one counted thousands of retired
    inboxes on each call: 2,872 of 2,910 rows on the live bus on 2026-11-04."""
    wired.registry.declare(NativeThread(
        name="retired", tags=frozenset(), worktree=Checkout(wired.root / "retired")))
    wired.messaging.send("PR111", "retired", "a message it will never read")
    wired.registry.archive("retired")

    user = wired.registry.claim_user(Checkout(wired.root / "user"))

    listed = {row.thread.name for row in wired.views.list_threads()}

    assert "retired" not in listed
    assert {"PR111", user.name} <= listed, "the roster lists the human, who never executes a turn"
