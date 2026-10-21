"""Thread read answers own comparison, retry and status projection."""
from itertools import product

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
