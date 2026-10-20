"""A stale open view is a registry-owned deletion result, not observer failure."""
import asyncio
import pickle
from dataclasses import replace

from agent_comms.activity import ActivityState, ObservedActivity
from agent_comms.registry_document import DEFAULT_ROSTER
from agent_comms.registry_provenance import DeletedThread
from agent_comms.thread_presentation import ThreadRetired
from agent_comms.ui_model.observation import (
    ObservationProcess, ObserveThreads, ObserveViews, ReadNotifications,
    ThreadMissing, ThreadsObserved, ViewsRetired,
)
from agent_comms.ui_model.status import OpenThreadStatus
from test_observer_death import readable


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
    comms.agents.set_activity('surviving', ActivityState.IDLE)
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
    comms.agents.set_activity('surviving', ActivityState.IDLE)
    list(reading.read(comms, comms.views.revision()))
    assert reading.observed.activity['surviving'].timestamp > previous
    assert reading.observed.activity['deleted'] == DeletedThread('deleted')


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


async def test_observer_process_survives_deleted_interest_and_keeps_serving(comms):
    declare(comms, 'surviving', 'deleted')
    original = comms.registry.require('deleted').incarnation
    names = frozenset({'surviving', 'deleted'})
    with ObservationProcess(comms.route) as process:
        process.request(ObserveThreads(names))
        process.request(ObserveViews(frozenset({original}), frozenset()))

        async def until(accepted):
            async with asyncio.timeout(30):
                while True:
                    await readable(process)
                    for result in process.results():
                        if accepted(result):
                            return result

        await until(lambda result: isinstance(result, ThreadsObserved) and result.changed == names)
        comms.registry.remove('deleted')
        missing = None
        retired = None
        async with asyncio.timeout(30):
            while missing is None or retired is None:
                await readable(process)
                for result in process.results():
                    if isinstance(result, ThreadsObserved) and 'deleted' in result.changed:
                        missing = result
                    elif isinstance(result, ViewsRetired):
                        retired = result
        assert missing.activity['deleted'] == DeletedThread('deleted')
        assert isinstance(missing.results['deleted'], ThreadMissing)
        assert retired.retired.threads == frozenset({original})
        surviving = comms.registry.require('surviving')
        comms.registry.declare(replace(surviving, title='Still updating after deletion'))
        updated = await until(lambda result: isinstance(result, ThreadsObserved)
                              and 'surviving' in result.changed)
        assert isinstance(updated.activity['surviving'], ObservedActivity)
        assert process.process.is_alive()
        loop = asyncio.get_running_loop()
        loop.add_reader(process.fileno(), process.results)
        try:
            async with asyncio.timeout(30):
                assert await process.read(ReadNotifications(str(comms.root), ())) == {}
        finally:
            loop.remove_reader(process.fileno())
