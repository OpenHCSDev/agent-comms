"""Fresh roots refuse unproven source coverage and unconfigured native owners."""

from agent_comms.channel_targets import CommandTarget

from agent_comms.active_route import resolve_comms_route

from agent_comms.private_runtime_schema import PrivateCoordinatorTable
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.channels import AnyChannel
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked
from agent_comms.proven_source_coverage import SourceCoverage
from agent_comms.cursor_owner import UnstartedCursor
from bus_record_fixture import prepare_native_comms
from test_private_human_ingress import _root


def test_fresh_root_claims_source_gaps_and_empty_cursor(tmp_path, *, source_reads):
    comms, store, root_id, lookups = _root(tmp_path)
    with store:
        PrivateCoordinatorTable.install(store)
        assert comms.bus.bus.revision() == 0
        empty = SourceCoverage(
            comms.bus, store, wire_root_id=root_id, recipient_lookup=lookups["bob"]
        ).since(UnstartedCursor(), source_reads=source_reads)
        assert empty.covered_seq == 0 and empty.injected_source_seqs == ()
        sent = comms.messaging.send_user_message(CommandTarget.named('bob'), "fresh selected input", worktree=str(tmp_path))
        reader = SourceCoverage(
            comms.bus, store, wire_root_id=root_id, recipient_lookup=lookups["bob"]
        )
        before = reader.since(UnstartedCursor(), source_reads=source_reads)
        assert before.blocked_seq == sent.seq and before.injected_source_seqs == ()
        accept_delivery_cohort(comms.bus, root_id, sent.seq, store)
        selected = reader.since(UnstartedCursor(), source_reads=source_reads)
        assert selected.blocked_seq == sent.seq  # Selection cannot manufacture native proof.
        assert selected.covered_seq == 0 and selected.injected_source_seqs == ()
        assert Comms(resolve_comms_route(comms.root)).bus.bus.revision() == sent.seq


def test_truncated_coverage_cannot_hide_an_unproven_source(tmp_path, *, source_reads):
    comms, store, root_id, lookups = _root(tmp_path)
    with store:
        PrivateCoordinatorTable.install(store)
        first = comms.messaging.send_user_message(CommandTarget.named('bob'), "first original", worktree=str(tmp_path))
        second = comms.messaging.send_user_message(CommandTarget.named('bob'), "second original", worktree=str(tmp_path))
        reader = SourceCoverage(
            comms.bus, store, wire_root_id=root_id, recipient_lookup=lookups["bob"]
        )
        with pytest.raises(IdentityConflict):
            reader.read(limit=1, source_reads=source_reads)
        page = reader.read(limit=1, partial=True, source_reads=source_reads)
        assert page.more_sources and page.blocked_seq == first.seq
        assert page.covered_seq == 0 and page.injected_source_seqs == ()
        assert [message.seq for message in comms.views.history_page(AnyChannel.target()).messages] == [first.seq, second.seq]
        with pytest.raises(ValueError):
            reader.read(limit=True, source_reads=source_reads)


async def test_unconfigured_acp_attach_refuses_before_creating_owner(tmp_path):
    comms = Comms(resolve_comms_route(tmp_path / "wire"))
    agent = CommsAgent(comms)
    with pytest.raises(PublicationActivationBlocked):
        await agent.new_session(cwd=str(tmp_path))
    assert not comms.registry.snapshot().threads


@pytest.mark.skipif(
    not os.environ.get("AC_NATIVE_COPIED_PACKAGE"),
    reason="Prepared native package required; no provider request",
)
async def test_configured_acp_new_and_retained_attach_use_same_checked_root(tmp_path):
    comms = prepare_native_comms(Comms(resolve_comms_route(tmp_path / "wire")))
    agent = CommsAgent(comms, auto_wake=False)
    try:
        fresh = await agent.new_session(cwd=str(tmp_path))
        assert fresh.session_id in comms.registry
        await agent.load_session(cwd=str(tmp_path), session_id=fresh.session_id)
        assert agent.sessions.require(fresh.session_id) == fresh.session_id
        assert comms.bus.bus.revision() == 0
    finally:
        await agent.shutdown()
