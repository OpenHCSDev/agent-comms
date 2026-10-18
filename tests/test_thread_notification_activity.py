"""Activity invalidates only the open views containing the changed participants."""
from dataclasses import replace
import os

import pytest

from agent_comms.activity import ActivityState
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.presentation import MessageNotification, WireRevision
from agent_comms.private_path import FileRevision
from agent_comms.ui_model.observation import (
    NotificationsObserved, ObserveNotifications, ObserveThreads, ReadNotifications, ThreadPresented,
)
from conftest import publish_initial


def opened(tmp_path):
    root = tmp_path / 'wire'
    root.mkdir(mode=0o700)
    comms = Comms(root)
    names = frozenset(('sender', 'Alice', 'Bob', 'Carol', 'Dave'))
    for i, name in enumerate((*sorted(names), 'Recipient', 'Unrelated')):
        comms.registry.register(comms.registry.new_thread(
            name, frozenset(), str(tmp_path), created_at=17000.0 + i,
            process_identity=ProcessIdentity.capture(os.getpid())))
    comms.messaging.initialize_private_initial_protocol()
    for recipient in ('Alice', 'Bob', 'Carol', 'Dave', 'Recipient'):
        publish_initial(comms, 'sender', recipient, f'@{recipient} original message')
    publish_initial(comms, 'Bob', 'Alice', '@Alice independent incoming message')
    reading = ObserveThreads(names).observation()
    revision = comms.views.revision()
    list(reading.read(comms, revision))
    list(reading.read(comms, revision))
    assert all(isinstance(result, ThreadPresented) for result in reading.observed.results.values())
    return comms, reading, revision


@pytest.mark.parametrize('actor', ['Unrelated', 'Recipient'])
def test_activity_only_reads_views_addressing_the_actor(tmp_path, monkeypatch, actor):
    comms, reading, revision = opened(tmp_path)
    previous = reading.observed
    expected = previous.results['sender'].presentation
    calls = []
    original = MessageNotification.references

    def references(cls, root, registry, log, selected):
        calls.append(selected)
        return original(root, registry, log, selected)

    monkeypatch.setattr(MessageNotification, 'references', classmethod(references))
    comms.agents.set_activity(actor, ActivityState.WORKING, 'Tool start')
    current = replace(comms.views.revision(), expiry_tick=revision.expiry_tick)
    list(reading.read(comms, current))
    if actor == 'Unrelated':
        assert not calls
    else:
        assert 'Recipient' in previous.activity  # No open tab is required for a recipient.
        assert calls == [tuple(dict.fromkeys((*expected.incoming_references,
                                             *expected.sent_references)))]
    assert reading.observed.results['Carol'] is previous.results['Carol']
    assert reading.observed.results['Dave'] is previous.results['Dave']


@pytest.mark.parametrize('change, reads', [
    ('Unrelated', False), ('Recipient', True), ('registry', True), ('reads', True), ('notifications', True),
])
def test_a_notification_window_rereads_only_for_its_participants_or_a_window_change(
        tmp_path, monkeypatch, change, reads):
    comms, reading, revision = opened(tmp_path)
    view = reading.observed.results['sender'].presentation
    request = ReadNotifications(str(comms.root), (*view.incoming_references, *view.sent_references))
    window = ObserveNotifications(frozenset({request})).observation()
    (first,) = window.read(comms, revision)
    assert isinstance(first, NotificationsObserved) and 'Recipient' in window.read_states[request].activity
    assert not list(window.read(comms, revision))
    calls = []
    original = comms.views.message_notifications_for_references

    def counted(references):
        calls.append(references)
        return original(references)

    monkeypatch.setattr(comms.views, 'message_notifications_for_references', counted)
    if change in ('Unrelated', 'Recipient'):
        comms.agents.set_activity(change, ActivityState.WORKING, 'Tool start')
        current = replace(comms.views.revision(), expiry_tick=revision.expiry_tick)
        assert current.activity != revision.activity
    else:
        marker = tmp_path / 'changed-source'
        marker.write_text('A different captured revision')
        current = replace(revision, **{change: (FileRevision.capture(marker),)})
    steps = list(window.read(comms, current))
    assert calls == ([request.references] if reads else [])
    assert [type(step) for step in steps] == ([NotificationsObserved] if reads else [])


@pytest.mark.parametrize('source', ['registry', 'reads', 'notifications', 'expiry_tick'])
def test_global_notification_sources_still_refresh_all_views(tmp_path, monkeypatch, source):
    comms, reading, revision = opened(tmp_path)
    selected = []
    original = MessageNotification.references

    def references(cls, root, registry, log, items):
        selected.extend(items)
        return original(root, registry, log, items)

    monkeypatch.setattr(MessageNotification, 'references', classmethod(references))
    if source == 'expiry_tick':
        current = replace(revision, expiry_tick=revision.expiry_tick + WireRevision.expiry_seconds)
    else:
        marker = tmp_path / 'changed-source'
        marker.write_text('A different captured revision')
        current = replace(revision, **{source: (FileRevision.capture(marker),)})
    assert current.notifications_changed_since(revision)
    changed = current.changed_threads(reading.observed, comms, reading.observed.activity)
    if source == 'registry':
        assert changed == reading.observed.results.keys()
    else:
        expected = {reference for result in reading.observed.results.values()
                    for reference in (*result.presentation.incoming_references,
                                      *result.presentation.sent_references)}
        assert set(selected) == expected
