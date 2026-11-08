"""A snapshot the viewer never needed is never written.

Each observation result is a complete state rather than a delta, so a later one for the
same question answers everything an unwritten earlier one would have. On a phone profiled
while scrolling, decoding pushed observation frames was 37.8% of the UI process's worker
threads, and every superseded frame cost bytes, a parse, a construction and a delivery
for a state that was already out of date when it arrived.

Replacement is only ever offered for the last unwritten result, so anything already
behind an ordered event stays behind it.
"""

from agent_comms.coordination.errors import DeferredRead
from agent_comms.repo import Checkout
from agent_comms.ui_model.observation import (
    ObserveSidebar,
    SidebarObserved,
    SidebarPreparing,
    SidebarUnreadable,
    UnreadableHistory,
    UnwrittenResults,
    ViewsRetired,
)
from agent_comms.history_views import RetiredViews
from agent_comms.ui_model.sidebar import SidebarModel


def sidebar(comms, request) -> SidebarObserved:
    snapshot = comms.views.viewer_snapshot(request.worktree)
    return SidebarObserved(comms.views.revision(), request, snapshot,
                           SidebarModel.derive(snapshot))


def test_a_later_sidebar_replaces_the_one_it_answers_for(comms):
    """Both are complete states of the same selection, so the viewer loses nothing."""
    request = ObserveSidebar(Checkout(comms.root), True, False)
    unwritten = UnwrittenResults()

    unwritten.offer(sidebar(comms, request))
    later = sidebar(comms, request)
    unwritten.offer(later)

    assert unwritten.queued == [later]


def test_another_selection_is_another_question(comms):
    """A sidebar for a different request answers nothing the first one would have."""
    here = ObserveSidebar(Checkout(comms.root), True, False)
    elsewhere = ObserveSidebar(Checkout(comms.root.parent), True, False)
    unwritten = UnwrittenResults()

    first = sidebar(comms, here)
    unwritten.offer(first)
    unwritten.offer(sidebar(comms, elsewhere))

    assert len(unwritten.queued) == 2
    assert unwritten.queued[0] is first


def test_an_unfinished_read_does_not_replace_a_finished_one(comms):
    """A preparing result explains a read in progress; it would lose rows the complete
    one already holds, so it claims to replace nothing."""
    request = ObserveSidebar(Checkout(comms.root), True, False)
    unwritten = UnwrittenResults()

    complete = sidebar(comms, request)
    unwritten.offer(complete)
    unwritten.offer(SidebarPreparing(comms.views.revision(), request, cause=DeferredRead()))

    assert len(unwritten.queued) == 2
    assert unwritten.queued[0] is complete


def test_nothing_is_passed_over_an_ordered_event(comms):
    """A retirement explains why rows vanished. A snapshot arriving after it is queued
    behind it, never in place of it and never in front of it: only the last unwritten
    result is ever offered for replacement."""
    request = ObserveSidebar(Checkout(comms.root), True, False)
    unwritten = UnwrittenResults()

    first = sidebar(comms, request)
    retirement = ViewsRetired(comms.views.revision(), RetiredViews(frozenset(), frozenset({'#gone'})))
    unwritten.offer(first)
    unwritten.offer(retirement)
    unwritten.offer(sidebar(comms, request))

    assert len(unwritten.queued) == 3
    assert unwritten.queued[0] is first
    assert unwritten.queued[1] is retirement


def test_a_complete_state_does_not_replace_an_unwritten_failure(comms):
    """A failure is not superseded by a later success for the same request.

    SidebarUnreadable says the read failed; a complete state says the rows are fine. It
    does not say the read failed, so the failure crosses the wire on its own. It inherited
    the permission from the shared sidebar parent until codex-latency read the pair.
    """
    request = ObserveSidebar(Checkout(comms.root), True, False)
    unwritten = UnwrittenResults()

    failure = SidebarUnreadable(comms.views.revision(), request,
                                  error=UnreadableHistory('gone', 'unreadable'))
    unwritten.offer(failure)
    unwritten.offer(sidebar(comms, request))

    assert len(unwritten.queued) == 2
    assert unwritten.queued[0] is failure
