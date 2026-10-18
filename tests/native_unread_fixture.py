"""Await the actual native preparation in synchronous saved-state assertions."""


def completed_counts(state, viewer, sources):
    for _ in range(50):
        result = state.counts(viewer, sources, document=state.reads.read())
        if not result.pending:
            return result
        preparation = state.preparation
        assert preparation is not state._idle, "Native counts are pending without authorized preparation"
        preparation.future.result(timeout=10)
    raise AssertionError("Native preparation did not cover the fixture")


def completed_snapshot(comms, worktree, **options):
    for _ in range(50):
        snapshot = comms.views.viewer_snapshot(worktree, **options)
        if not snapshot.unread_pending:
            return snapshot
        preparation = comms.views.transcript_reads.preparation
        assert preparation is not comms.views.transcript_reads._idle, "Snapshot is pending without native preparation"
        preparation.future.result(timeout=10)
    raise AssertionError("Native preparation did not cover the fixture")
