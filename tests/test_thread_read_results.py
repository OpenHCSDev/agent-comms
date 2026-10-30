"""Thread read answers own comparison, retry and status projection."""
from agent_comms.repo import Checkout
from dataclasses import replace
from itertools import product
import sys

import pytest

from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.presentation import WireRevision
from agent_comms.read_ledger import ReadLedger
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


@pytest.mark.parametrize('changed_name, expected', [
    ('beta', {'beta'}), ('outside', set()), ('read', {'beta'}), ('unrelated_read', set()),
    ('expiry', set()),
])
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
    from agent_comms.notification_read import NotificationsRead
    from agent_comms.notification_reads import LiveNotificationRead

    root, root_id, comms, initial, _people = _root(private_directory, direct=True)
    for name in ('gamma', 'outside'):
        comms.registry.declare(Thread(name, frozenset(), Checkout(private_directory)))
    for name in ('alpha', 'gamma', 'outside'):
        message = publish_initial(comms, 'sender', name, f'Work for {name}')
        with Coordination(str(root / 'coordination.sqlite3')) as store:
            store.participants.register(comms.registry.require(name).incarnation.lookup, committed=True)
            accept_initial(comms.bus, root_id, message.seq, store)
    notification_source = initial.message
    if changed_name in ('read', 'unrelated_read'):
        human = comms.messaging.user_identity(Checkout(private_directory))
        read_source = publish_initial(comms, 'beta' if changed_name == 'read' else 'outside',
                                      human.name, 'Human read receipt')
        if changed_name == 'read':
            notification_source = read_source
    names = frozenset(('alpha', 'beta', 'gamma'))
    batch = comms.views.thread_presentations(names)
    results = {name: ThreadPresented(batch.require(name)) for name in names}
    revision = comms.views.revision()
    activity = comms.views.observed_activity(revision, names)
    previous = ThreadsObserved(revision, results, names, activity)
    notification_window = LiveNotificationRead((notification_source.reference,)).read(comms)
    notification_read = NotificationsRead.of(revision, comms, activity, notification_window)
    if changed_name in ('read', 'unrelated_read'):
        ledger = comms.bus.reads
        basis = ledger.capture(human.name, (read_source,), comms.registry.snapshot(),
                               comms.bus.bus, document=ledger.read())
        ledger.mark_displayed(human.name, basis)
    elif changed_name != 'expiry':
        with Coordination(str(root / 'coordination.sqlite3')) as store:
            assignment, = sealed_cohort_assignments(
                store, comms.registry.require(changed_name).incarnation.lookup,
            )
            store.assignments.transition_preengagement(
                assignment.assignment_id, DeferredAssignment, expected_revision=assignment.revision,
            )
    later = comms.views.revision()
    if changed_name == 'expiry':
        later = replace(later, expiry_tick=revision.expiry_tick + 60)
    assert later != revision
    calls = 0
    evidence_calls = 0
    evidence_codes = {NotificationAssignment.changed.__func__.__code__,
                      ReadLedger.changed_notifications.__code__}
    projection_code = MessageNotification.delivery_window.__func__.__code__

    def count_projection(frame, event, argument):
        nonlocal calls, evidence_calls
        if event == 'call':
            if frame.f_code is projection_code:
                calls += 1
            if frame.f_code in evidence_codes:
                evidence_calls += 1

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
    if changed_name == 'expiry':
        assert evidence_calls == 0


def reachable_types(root: object) -> set[str]:
    """Class names of every instance reachable from root, not following classes,
    modules or functions."""
    import gc
    import types

    seen, names, frontier = set(), set(), [root]
    skipped = (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType,
               types.MethodType)
    while frontier:
        item = frontier.pop()
        if id(item) in seen or isinstance(item, skipped):
            continue
        seen.add(id(item))
        names.add(type(item).__name__)
        frontier.extend(gc.get_referents(item))
    return names


def pickled_types(value: object) -> set[str]:
    """Class names of every object the transport pickles for value."""
    import io
    import pickle

    names: set[str] = set()

    class Recording(pickle.Pickler):
        def reducer_override(self, obj):
            names.add(type(obj).__name__)
            return NotImplemented

    Recording(io.BytesIO(), protocol=pickle.HIGHEST_PROTOCOL).dump(value)
    return names


ACQUIRED_EVIDENCE = {
    "AcquiredThreadPresentation", "CapturedNotificationWindow", "NotificationAssignmentRead",
    "SourceEvidence", "WakeAssignment", "AssignmentActivityEvidence", "HarnessDelivery",
    "Delivered", "NotificationReadMark",
}


def test_thread_reading_keeps_acquired_evidence_and_publishes_its_projection(private_directory):
    import pickle

    from conftest import publish_initial
    from test_coordinated_runtime import _root
    from agent_comms.thread_presentation import AcquiredThreadPresentation, ThreadPresentation

    _path, _root_id, comms, initial, _people = _root(private_directory, direct=True)
    reading = ObserveThreads(frozenset({'beta'})).observation()
    status = OpenThreadStatus(lambda flush: None)
    status.open('beta')

    def observe() -> ThreadsObserved:
        published = [step for step in reading.read(comms, comms.views.revision())
                     if type(step) is ThreadsObserved]
        assert len(published) == 1
        status.observed(published[0])
        status.rows.flush()
        return published[0]

    first = observe()
    retained = reading.observed.results['beta'].presentation
    assert type(retained) is AcquiredThreadPresentation
    assert retained.incoming_references
    outgoing = first.results['beta'].presentation
    assert type(outgoing) is ThreadPresentation
    assert outgoing == retained.published()
    assert outgoing.status_display() == retained.status_display()
    assert outgoing.read_identity is retained.read_identity
    assert outgoing.notifications == retained.notifications
    incoming = [item for item in outgoing.notifications if item.message.seq == initial.message.seq]
    assert incoming and all(item.message == initial.envelope for item in incoming)
    assert not ACQUIRED_EVIDENCE & pickled_types(first)
    with pytest.raises(TypeError, match="published"):
        pickle.dumps(replace(first, results={'beta': ThreadPresented(retained)}))
    assert (ThreadStatusModel.known('beta', outgoing).lines
            == ThreadStatusModel.known('beta', retained).lines)

    publish_initial(comms, 'sender', 'beta', 'A later message for beta')
    second = observe()
    assert second.results['beta'].presentation != outgoing
    assert reading.observed.results['beta'].presentation is not retained
    # The UI's current and last-published rows hold projections only; the old acquired
    # value is reachable from neither.
    assert status.rows._published == status.rows.rows
    assert not ACQUIRED_EVIDENCE & reachable_types(status.rows)
    assert type(status.rows.rows['beta'].presentation) is ThreadPresentation
