"""Checkpoint commit owns current membership and consumed-operation history."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.envelope_claim_transitions import ClaimConflict, ExistingFileClaim, WakeAdmission
from agent_comms.messages import InfoKind, Message
from agent_comms.private_bus_checkpoint import CertifiedSourceRead, ClaimCommits
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.wire_log import WireLog


def authored(tmp_path):
    comms = Comms(tmp_path / 'wire')
    root_id = comms.messaging.initialize_private_initial_protocol()
    for name in ('owner', 'peer'):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    paths = tuple(tmp_path / name for name in ('one.txt', 'two.txt', 'three.txt'))
    for path in paths:
        path.write_text('owned\n')
    original = comms.messaging.send_message('peer', 'owner', 'original source')
    admission = WakeAdmission(root_id, original.seq, original.message_id,
        'cohort-v1:' + 'd' * 64, 1, 'e' * 32, 'execution-one', 'f' * 32,
        1, 'turn-one', 1, 1)
    return comms, paths, admission


def admitted(comms, admission, paths):
    with _store_lock(comms._wire_lock_path), comms.bus.log.locked() as lock:
        snapshot = comms.registry.snapshot()
        owner = snapshot.threads['owner']
        return comms.bus.publisher.publish_claim_envelope(
            Message('owner', 'peer', 'admitted claim', InfoKind),
            worktree=Path(owner.worktree), incarnation=str(owner.created_at),
            claims=tuple(ExistingFileClaim(path) for path in paths),
            _locked_registry_snapshot=snapshot, _bus_lock=lock, _admission=admission,
        )


def test_multi_resource_admission_consumes_one_operation_even_after_release(tmp_path):
    comms, paths, admission = authored(tmp_path)
    claimed = admitted(comms, admission, paths[:2])
    with comms.bus.log.certified_read() as source:
        commits = ClaimCommits.select(source.connection)
        assert len(commits) == 1 and commits[0].operation_id == admission.operation_id
        assert source.claim_operation_used(admission.operation_id)
        assert all(source.claim(str(path)).admission == admission for path in paths[:2])
    comms.messaging.send_message('owner', 'peer', 'release', releases=paths[:2])
    assert not comms.bus.log.claim_projection()
    before = comms.bus.log.path.read_bytes()
    with pytest.raises(IdentityConflict, match='already consumed'):
        admitted(comms, admission, paths[2:])
    assert comms.bus.log.path.read_bytes() == before
    reopened = Comms(comms.root)
    with reopened.bus.log.certified_read() as source:
        assert source.claim_operation_used(admission.operation_id)
        assert source.claim(str(paths[0])) is None
    again = admitted(reopened, replace(admission, operation_id='a' * 32), paths[:2])
    assert again.seq > claimed.seq


def test_claim_decisions_never_read_or_decode_retained_messages(tmp_path, monkeypatch):
    comms, paths, admission = authored(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError('A claim decision must not scan or decode bus history')

    monkeypatch.setattr(CertifiedSourceRead, 'messages', forbidden)
    monkeypatch.setattr(WireLog, 'verified_records_unlocked', forbidden)
    first = admitted(comms, admission, paths[:1])
    with pytest.raises(ClaimConflict):
        comms.messaging.send_message('peer', 'owner', 'losing', claims=[ExistingFileClaim(paths[0])])
    assert comms.bus.log.claim_projection()[str(paths[0])].seq == first.seq
    comms.messaging.send_message('owner', 'peer', 'release', releases=paths[:1])
    with pytest.raises(IdentityConflict):
        admitted(comms, admission, paths[1:2])
    assert not comms.bus.log.claim_projection()


def test_resource_and_operation_queries_use_their_unique_indexes(tmp_path):
    comms, paths, admission = authored(tmp_path)
    admitted(comms, admission, paths[:1])
    with comms.bus.log.certified_read() as source:
        statements = []
        source.connection.set_trace_callback(statements.append)
        assert source.claim(str(paths[0])) is not None
        assert source.claim_operation_used(admission.operation_id)
        source.connection.set_trace_callback(None)
        assert len(statements) == 2
        for sql in statements:
            plan = [row[3] for row in source.connection.execute('EXPLAIN QUERY PLAN ' + sql)]
            assert any('SEARCH' in detail and 'INDEX' in detail for detail in plan)
            assert not any('SCAN ' in detail for detail in plan)
