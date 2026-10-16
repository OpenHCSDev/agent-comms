"""Await the actual native preparation in synchronous saved-state assertions."""
from agent_comms.read_ledger import (
    AbsentReadSource, NoReadNotice, ReadLedger, TranscriptViewer,
)
from agent_comms.view_unread import TranscriptReadState


def transcript_fixture(path):
    """Explicitly create the standalone fixture's authority before opening a reader."""
    ledger = ReadLedger(path.parent)
    if not ledger.path.exists():
        ledger.create(AbsentReadSource(), NoReadNotice())
    return TranscriptReadState(path)


def counts(state, viewer, sources):
    document = state.reads.read(TranscriptViewer(viewer))
    return state.counts(viewer, sources, document=document)


def completed_counts(state, viewer, sources):
    document = state.reads.read(TranscriptViewer(viewer))
    for _ in range(50):
        result = state.counts(viewer, sources, document=document)
        if not result.pending:
            return result
        preparation = state.preparation
        assert preparation is not None, "Native counts are pending without admitted preparation"
        preparation.result(timeout=10)
    raise AssertionError("Native preparation did not cover the fixture")


def completed_snapshot(comms, worktree, **options):
    for _ in range(50):
        snapshot = comms.views.viewer_snapshot(comms.messaging.user_identity(worktree), **options)
        if not snapshot.unread_pending:
            return snapshot
        preparation = comms.views.transcript_reads.preparation
        assert preparation is not None, "Snapshot is pending without native preparation"
        preparation.result(timeout=10)
    raise AssertionError("Native preparation did not cover the fixture")
