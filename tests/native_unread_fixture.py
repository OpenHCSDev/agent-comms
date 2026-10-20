"""Await actual native index preparation; tests declare their own registry threads."""

from collections.abc import Mapping

from agent_comms.channel_targets import CommandTarget, ThreadTarget
from agent_comms.comms import wire
from agent_comms.ui_model.unread import UnreadPresentation
from agent_comms.registry_document import DEFAULT_ROSTER
from agent_comms.view_unread import TranscriptReadState


def dm_counts(unread: Mapping[CommandTarget, UnreadPresentation]) -> dict[str, int]:
    """Human DMs only; NativeThreadTarget subclasses ThreadTarget for saved replies."""
    return {reference.name: shown.count for reference, shown in unread.items()
            if type(reference) is ThreadTarget and shown.count}


def native_reader(path):
    return TranscriptReadState(path)


def completed_counts(state, viewer):
    for _ in range(50):
        result = state.counts(viewer, registry=wire(state.path.parent).registry.snapshot(),
                              document=state.reads.read(), visibility=DEFAULT_ROSTER)
        if state.preparation is state._idle:
            return result
        futures = state.watch(frozenset(), lambda _: None)
        assert futures, "Native counts are pending without authorized preparation"
        for future in futures:
            future.result(timeout=10)
    raise AssertionError("Native preparation did not cover the fixture")


def completed_snapshot(comms, worktree, **options):
    for _ in range(50):
        snapshot = comms.views.viewer_snapshot(worktree, **options)
        if not any(unread.pending for unread in snapshot.unread.values()) and comms.views.transcript_reads.preparation is comms.views.transcript_reads._idle:
            return snapshot
        futures = comms.views.transcript_reads.watch(frozenset(), lambda _: None)
        assert futures, "Snapshot is pending without native preparation"
        for future in futures:
            future.result(timeout=10)
    raise AssertionError("Native preparation did not cover the fixture")
