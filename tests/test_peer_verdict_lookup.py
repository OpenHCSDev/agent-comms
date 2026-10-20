"""Peer verdicts resolve old originals rather than searching a fixed bus tail."""

from types import SimpleNamespace

import pytest

from conftest import publish_initial
from agent_comms.comms import Comms
from agent_comms.messages import InfoKind, Message
from agent_comms.todos import Assignment, PeerAcceptedVerdict


def test_public_only_imported_order_is_not_lost(tmp_path):
    comms = Comms(tmp_path / 'imported')
    for name in ('author', 'worker', 'reviewer'):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    original = Message('author', 'worker', 'Review the imported old order', InfoKind, seq=1)
    comms.messaging.initialize_private_initial_protocol(history=(original,))
    with comms.bus.log.opened_wire_snapshot() as opened, comms.bus.log.checked_read() as source:
        assert source.message_by_id(original.message_id, public=opened) == original
    reviewer = comms.registry.require('reviewer')
    verdict = PeerAcceptedVerdict(
        1, reviewer.incarnation, Assignment('worker', 1.0, 'reviewer', 1.0, 'one'),
        'tool-call', quote='Review the imported old order', peer=original.message_id)
    assert verdict.prepare(SimpleNamespace(bus=comms.bus), SimpleNamespace(holder='worker')) is verdict


def test_peer_order_before_recent_three_megabytes_is_found(tmp_path):
    comms = Comms(tmp_path / 'bus')
    for name in ('author', 'worker', 'reviewer'):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    comms.messaging.initialize_private_initial_protocol()
    original = publish_initial(comms, 'author', 'worker', 'Fix the old retained order correctly')
    for number in range(5):
        publish_initial(comms, 'author', 'reviewer', f'{number}:' + 'x' * 750_000)
    with comms.bus.log.opened_wire_snapshot() as tail:
        assert original.message_id not in {message.message_id for message in tail.newest_messages()}
    with comms.bus.log.checked_read() as source:
        assert source.message_by_id(original.message_id) == original
        assert source.message_by_id('missing-peer-id') is None
    reviewer = comms.registry.require('reviewer')
    verdict = PeerAcceptedVerdict(
        1, reviewer.incarnation, Assignment('worker', 1.0, 'reviewer', 1.0, 'one'),
        'tool-call', quote='Fix the old retained order', peer=original.message_id)
    assert verdict.prepare(SimpleNamespace(bus=comms.bus), SimpleNamespace(holder='worker')) is verdict
    with pytest.raises(ValueError, match='does not exist'):
        PeerAcceptedVerdict(
            1, reviewer.incarnation, Assignment('worker', 1.0, 'reviewer', 1.0, 'one'),
            'tool-call', quote='Fix the old retained order', peer='missing-peer-id',
        ).prepare(SimpleNamespace(bus=comms.bus), SimpleNamespace(holder='worker'))
