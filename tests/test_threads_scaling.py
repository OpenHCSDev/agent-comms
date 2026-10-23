"""Thread-list pending counts agree with current routing after a rename."""

from agent_comms.channel_targets import CommandTarget
from agent_comms.threads import Thread


def test_rename_alias_and_real_thread_named_broadcast_match_existing_scope(wired):
    wired.registry.declare(Thread(
        name="broadcast", tags=frozenset(), worktree=str(wired.root / "broadcast"),
    ))
    wired.messaging.send("PR111", "broadcast", "direct message to the named thread")
    wired.messaging.send("fixer", "broadcast", "second sender")
    wired.messaging.send("PR111", "fixer", "old direct")
    wired.registry.rename("PR111", "renamed")

    def consistent():
        selected = wired.registry.all_threads()
        expected = {name: wired.bus.pending_count(name) for name in selected}
        assert {row.thread.name: row.pending.value for row in wired.views.list_threads()} == expected

    consistent()
    references = tuple(message.reference for message in wired.bus.inbox(
        "broadcast", CommandTarget.named("PR111"),
    ).page.messages)
    wired.messaging.acknowledge("broadcast", references)
    consistent()
