"""Await actual native index preparation; tests declare their own registry threads."""

from agent_comms.comms import wire
from agent_comms.registry_document import DEFAULT_ROSTER
from agent_comms.view_unread import TranscriptReadState


def native_reader(path):
    return TranscriptReadState(path, wire(path.parent).registry)


def completed_counts(state, viewer):
    for _ in range(50):
        result = state.counts(viewer, document=state.reads.read(), visibility=DEFAULT_ROSTER)
        if not result.pending and state.preparation is state._idle:
            return result
        preparation = state.preparation
        assert preparation is not state._idle, "Native counts are pending without authorized preparation"
        preparation.future.result(timeout=10)
    raise AssertionError("Native preparation did not cover the fixture")


def completed_snapshot(comms, worktree, **options):
    for _ in range(50):
        snapshot = comms.views.viewer_snapshot(worktree, **options)
        if not snapshot.unread_pending and comms.views.transcript_reads.preparation is comms.views.transcript_reads._idle:
            return snapshot
        preparation = comms.views.transcript_reads.preparation
        assert preparation is not comms.views.transcript_reads._idle, "Snapshot is pending without native preparation"
        preparation.future.result(timeout=10)
    raise AssertionError("Native preparation did not cover the fixture")
