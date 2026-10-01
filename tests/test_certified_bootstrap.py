"""Fresh canonical roots certify before publication or native source observation."""

import os
from pathlib import Path

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import PublicationActivationBlocked
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.errors import RelationViolationError
from agent_comms.private_bus_checkpoint import PrefixWitness, verify_private_bus_checkpoint_unlocked
from agent_comms.proven_source_coverage import SourceCoverage
from agent_comms.threads import Thread
from test_private_human_ingress import _root


def source_witness(bus):
    with bus.log.locked(blocking=False):
        return verify_private_bus_checkpoint_unlocked(bus.log, bus.log._private_marker_unlocked())


def test_fresh_root_claims_source_gaps_and_empty_cursor(tmp_path):
    comms, store, root_id, lookups = _root(tmp_path)
    with store:
        install_native_runtime_schema(store)
        witness = source_witness(comms.bus)
        assert isinstance(witness, PrefixWitness)
        assert witness.root_id == root_id and witness.through_seq == witness.offset == 0
        assert comms.bus.log.read_metadata_unlocked(required=True).claims
        empty = SourceCoverage(
            comms.bus, store, wire_root_id=root_id, recipient_lookup=lookups["bob"]
        ).prefix()
        assert empty.covered_seq == 0 and empty.injected_source_seqs == ()
        sent = comms.messaging.send_user_message(
            "bob", "fresh selected input", worktree=str(tmp_path)
        )
        before = SourceCoverage(
            comms.bus, store, wire_root_id=root_id, recipient_lookup=lookups["bob"]
        ).prefix()
        assert before.blocked_seq == sent.seq and before.injected_source_seqs == ()
        accept_delivery_cohort(comms.bus, root_id, sent.seq, store)
        selected = SourceCoverage(
            comms.bus, store, wire_root_id=root_id, recipient_lookup=lookups["bob"]
        ).prefix()
        assert selected.blocked_seq == sent.seq  # Selection cannot manufacture native proof.
        assert selected.covered_seq == 0 and selected.injected_source_seqs == ()
        assert source_witness(Comms(comms.root).bus).through_seq == sent.seq


def test_truncated_coverage_cannot_hide_an_unproven_source(tmp_path):
    comms, store, root_id, lookups = _root(tmp_path)
    with store:
        install_native_runtime_schema(store)
        first = comms.messaging.send_user_message("bob", "first original", worktree=str(tmp_path))
        second = comms.messaging.send_user_message("bob", "second original", worktree=str(tmp_path))
        reader = SourceCoverage(
            comms.bus, store, wire_root_id=root_id, recipient_lookup=lookups["bob"]
        )
        with pytest.raises(IdentityConflict, match="bounded private initial scan"):
            reader.read(limit=1)
        page = reader.read(limit=1, partial=True)
        assert page.more_sources and page.blocked_seq == first.seq
        assert page.covered_seq == 0 and page.injected_source_seqs == ()
        assert [message.seq for message in comms.bus.log.full_history()] == [first.seq, second.seq]
        with pytest.raises(ValueError, match="received bool"):
            reader.read(limit=True)


def test_failed_checkpoint_bootstrap_never_commits_registry_guard(tmp_path, monkeypatch):
    from agent_comms import wire_log

    comms = Comms(tmp_path / "wire")
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path)))
    write = wire_log._atomic_write_text

    def fail_seal(path, contents, **kwargs):
        if Path(path).name == "bus_meta.json" and "checkpoint_seal" in contents:
            raise OSError("checkpoint seal disk failure")
        return write(path, contents, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(wire_log, "_atomic_write_text", fail_seal)
        with pytest.raises(OSError, match="checkpoint seal disk failure"):
            comms.messaging.initialize_private_initial_protocol()
    assert (comms.root / "private_bus_checkpoint.sqlite3").exists()
    with pytest.raises(RelationViolationError, match="pending"):
        Comms(comms.root).registry.snapshot()
    with pytest.raises(RelationViolationError):
        source_witness(comms.bus)
    assert comms.bus.log.path.read_bytes() == b""


async def test_unconfigured_acp_attach_refuses_before_creating_owner(tmp_path):
    comms = Comms(tmp_path / "wire")
    agent = CommsAgent(comms)
    with pytest.raises(PublicationActivationBlocked, match="explicit matching root and package"):
        await agent.new_session(cwd=str(tmp_path))
    assert not comms.registry.snapshot().threads


@pytest.mark.skipif(
    not os.environ.get("AC_NATIVE_COPIED_PACKAGE"),
    reason="Prepared native package required; no provider request",
)
async def test_configured_acp_new_and_retained_attach_use_same_certified_root(tmp_path):
    comms = Comms(tmp_path / "wire")
    root_id = comms.messaging.initialize_private_initial_protocol()
    package = Path(os.environ["AC_NATIVE_COPIED_PACKAGE"])
    agent = CommsAgent(
        comms,
        auto_wake=False,
        private_nk_wire_root_id=root_id,
        private_nk_native_package=package,
    )
    try:
        fresh = await agent.new_session(cwd=str(tmp_path))
        assert fresh.session_id in comms.registry
        await agent.load_session(cwd=str(tmp_path), session_id=fresh.session_id)
        assert agent.sessions.require(fresh.session_id) == fresh.session_id
        assert source_witness(comms.bus).through_seq == 0
    finally:
        await agent.shutdown()
