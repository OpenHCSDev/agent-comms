"""Declare native fixture threads and await their actual index preparation."""

from dataclasses import replace

from agent_comms.comms import wire
from agent_comms.view_unread import TranscriptReadState


def native_reader(path):
    return TranscriptReadState(path, wire(path.parent).registry)


def reader_counts(state, viewer, sources, *, document=None):
    registry = state.registry
    threads = registry.snapshot().threads
    for name in threads.keys() - sources.keys():
        if tuple(threads[name].role.executor(threads[name])):
            registry.remove(name)
    for name, files in sources.items():
        source, = files
        current = threads.get(name)
        if current is None:
            registry.declare(registry.new_thread(name, frozenset(), str(state.path.parent), session_file=source))
        elif current.session_file != source:
            registry.register(replace(current, session_file=source))
    return state.counts(viewer, document=state.reads.read() if document is None else document)


def completed_counts(state, viewer, sources):
    for _ in range(50):
        result = reader_counts(state, viewer, sources, document=state.reads.read())
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
