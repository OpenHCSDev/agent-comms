"""A stale open view is a registry-owned deletion result, not observer failure."""
import pickle

from agent_comms.activity import ActivityState, ObservedActivity
from agent_comms.registry_document import DEFAULT_ROSTER
from agent_comms.registry_provenance import DeletedThread
from agent_comms.thread_presentation import ThreadRetired
from agent_comms.ui_model.observation import ObserveThreads, ThreadMissing, ThreadsObserved
from agent_comms.ui_model.status import OpenThreadStatus


def declare(comms, *names):
    for name in names:
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(comms.root.parent)))
    comms.messaging.initialize_private_initial_protocol()


def observe(reading, comms):
    return next(result for result in reading.read(comms, comms.views.revision())
                if isinstance(result, ThreadsObserved))


def test_observer_reports_a_deleted_thread(comms):
    declare(comms, 'surviving', 'deleted')
    names = frozenset({'surviving', 'deleted'})
    reading = ObserveThreads(names).observation()
    status = OpenThreadStatus(lambda callback: callback())
    status.observed(observe(reading, comms))
    comms.registry.remove('deleted')
    comms.agents.set_activity('surviving', ActivityState.IDLE, 'after deletion')
    snapshot = comms.registry.snapshot()
    assert tuple(thread.name for thread in snapshot.visible_threads(DEFAULT_ROSTER, names=names)) == ('surviving',)
    result = pickle.loads(pickle.dumps(observe(reading, comms)))
    assert isinstance(result.activity['surviving'], ObservedActivity)
    assert result.activity['surviving'].thread == 'surviving'
    assert result.activity['deleted'] == DeletedThread('deleted')
    assert isinstance(result.results['deleted'], ThreadMissing)
    status.observed(result)
    assert isinstance(status.rows.rows['deleted'].status, ThreadRetired)
    previous = result.activity['surviving'].timestamp
    comms.agents.set_activity('surviving', ActivityState.IDLE, 'still updating')
    updated = observe(reading, comms)
    assert updated.activity['surviving'].timestamp > previous
    assert updated.activity['deleted'] == DeletedThread('deleted')


def test_snapshot_keeps_alias_activity_and_reports_names_absent_after_deletion(comms):
    declare(comms, 'original', 'surviving')
    comms.registry.rename('original', 'renamed')
    names = ('original', 'renamed', 'surviving')
    before = comms.registry.snapshot()
    acquired = before.observed_activity(comms.agents, names)
    assert acquired['original'] == acquired['renamed']
    assert acquired['original'].thread == 'renamed'
    comms.registry.remove('renamed')
    after = comms.registry.snapshot()
    acquired = after.observed_activity(comms.agents, names)
    assert acquired['original'] == DeletedThread('original')
    assert acquired['renamed'] == DeletedThread('renamed')
    assert isinstance(acquired['surviving'], ObservedActivity)
    # The old immutable cut still declares the original thread; the new cut does not.
    assert tuple(thread.name for thread in before.visible_threads(DEFAULT_ROSTER, names=names)) == ('renamed', 'surviving')
    assert tuple(thread.name for thread in after.visible_threads(DEFAULT_ROSTER, names=names)) == ('surviving',)
