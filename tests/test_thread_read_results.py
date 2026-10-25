"""Thread read answers own comparison, retry and status projection."""
from itertools import product
import sys

import pytest

from agent_comms.presentation import WireRevision
from agent_comms.thread_presentation import NoThreadPresentation
from agent_comms.ui_model.observation import (
    NotObserved, ObserveThreads, ThreadBusy, ThreadPresented, ThreadUnavailable, ThreadsObserved,
    UnreadableHistory,
)
from agent_comms.ui_model.status import OpenThreadStatus, ThreadStatusModel


def answers():
    return (ThreadPresented(ThreadStatusModel.pending('saved').presentation),
            ThreadUnavailable(UnreadableHistory.capture(ValueError('broken'))), ThreadBusy(),
            ThreadPresented(NoThreadPresentation()))


def observed(results):
    return ThreadsObserved(WireRevision.empty(), results, frozenset(results), {})


def test_result_transitions_and_missing_keys():
    values = answers()
    for left, right in product(range(4), repeat=2):
        current, previous = observed({'saved': values[left]}), observed({'saved': values[right]})
        assert current.changed_since(previous) == (frozenset() if left == right else frozenset({'saved'}))
    assert observed({'saved': values[0]}).changed_since(observed({})) == {'saved'}
    assert observed({}).changed_since(observed({'saved': values[0]})) == {'saved'}


def test_status_projection_and_busy_retains_the_canonical_row():
    status = OpenThreadStatus(lambda callback: callback())
    presentation = ThreadStatusModel.pending('saved').presentation
    status.observed(observed({'saved': ThreadPresented(presentation)}))
    before = status.open('saved')
    status.observed(observed({'saved': ThreadBusy()}))
    assert status.open('saved') is before
    failure = answers()[1]
    status.observed(observed({'saved': failure}))
    assert 'broken' in status.open('saved').lines[0]
    status.observed(observed({'saved': ThreadPresented(NoThreadPresentation())}))
    shown = status.open('saved').presentation
    assert isinstance(shown, NoThreadPresentation)
    assert shown.same_status(ThreadStatusModel.retired('saved').presentation)


def test_changed_interest_retains_one_results_mapping_and_starts_empty():
    first = ObserveThreads(frozenset({'one'})).observation()
    assert first.observed.results == {}
    first.observed = observed({'one': ThreadBusy()})
    assert NotObserved().for_interest(first) is first
    assert first.for_interest(ObserveThreads(frozenset({'one'})).observation()) is first
    next_read = first.for_interest(ObserveThreads(frozenset({'one', 'two'})).observation())
    assert next_read.observed is first.observed
    assert next_read.initial == {'two'}


@pytest.mark.parametrize('changed_name, expected', [('beta', {'beta'}), ('outside', set())])
def test_assignment_row_changes_attribute_only_their_view_without_projection(
    private_directory, changed_name, expected,
):
    from conftest import accept_initial, publish_initial
    from test_coordinated_runtime import _root
    from agent_comms.assignment_states import DeferredAssignment
    from agent_comms.coordination_cohort import sealed_cohort_assignments
    from agent_comms.coordinator import Coordination
    from agent_comms.presentation import MessageNotification
    from agent_comms.threads import Thread
    from agent_comms.ui_model.observation import NotificationsRead, ReadNotifications

    root, root_id, comms, initial, _people = _root(private_directory, direct=True)
    for name in ('gamma', 'outside'):
        comms.registry.declare(Thread(name, frozenset(), str(private_directory)))
    for name in ('alpha', 'gamma', 'outside'):
        message = publish_initial(comms, 'sender', name, f'Work for {name}')
        with Coordination(str(root / 'coordination.sqlite3')) as store:
            store.participants.register(comms.registry.require(name).incarnation.lookup, committed=True)
            accept_initial(comms.bus, root_id, message.seq, store)
    names = frozenset(('alpha', 'beta', 'gamma'))
    batch = comms.views.thread_presentations(names)
    results = {name: ThreadPresented(batch.require(name)) for name in names}
    revision = comms.views.revision()
    activity = comms.views.observed_activity(revision, names)
    previous = ThreadsObserved(revision, results, names, activity)
    notification_window = ReadNotifications(root_id, (initial.message.reference,)).read(comms)
    notification_read = NotificationsRead.of(revision, comms, activity, notification_window)
    with Coordination(str(root / 'coordination.sqlite3')) as store:
        assignment, = sealed_cohort_assignments(
            store, comms.registry.require(changed_name).incarnation.lookup,
        )
        store.assignments.transition_preengagement(
            assignment.assignment_id, DeferredAssignment, expected_revision=assignment.revision,
        )
    later = comms.views.revision()
    assert later.notifications != revision.notifications
    calls = 0
    projection_code = MessageNotification.delivery_window.__func__.__code__

    def count_projection(frame, event, argument):
        nonlocal calls
        if event == 'call' and frame.f_code is projection_code:
            calls += 1

    profiling = sys.getprofile()
    sys.setprofile(count_projection)
    try:
        changed = later.changed_threads(previous, comms, activity)
        notification_changed = notification_read.live_changed(comms, later)
    finally:
        sys.setprofile(profiling)
    assert changed == expected
    assert notification_changed == bool(expected)
    assert calls == 0
