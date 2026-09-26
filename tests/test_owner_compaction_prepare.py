"""Provider-free read-only preparation from the exact disposable Pi tree."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.backend import PersistentPiSession
from agent_comms.declarations import Goal, RelationViolationError, Thread, ThreadRegistry
from agent_comms.operations import Comms
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from agent_comms.owner_compaction_prepare import NativePreparationError, prepare_native_source
from agent_comms.owner_compaction_runtime import compact_owner_once

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(
    not PACKAGE or sys.platform != "linux", reason="Disposable Linux native opt-in"
)


@pytest.fixture
def session(tmp_path):
    script = """
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const {SessionManager} = await import(pathToFileURL(join(process.argv[1],
  'dist/core/session-manager.js')));
const manager = SessionManager.create(process.argv[2], join(process.argv[2], 'sessions'));
manager.appendMessage({role:'user', content:'First task '+('history '.repeat(500)), timestamp:1});
manager.appendMessage({role:'assistant', content:[{type:'text',text:'First answer'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:2});
manager.appendMessage({role:'user', content:'Next task', timestamp:3});
manager.appendMessage({role:'assistant', content:[{type:'text',text:'Next answer'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:4});
console.log(manager.getSessionFile());
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, PACKAGE, str(tmp_path)],
        capture_output=True,
        check=True,
        timeout=5,
        text=True,
    )
    return Path(result.stdout.strip())


def test_native_preparation_is_read_only_and_matches_saved_cutpoint(session):
    original = session.read_bytes()
    package = Path(PACKAGE)
    prepared = prepare_native_source(package, str(session), keep_recent_tokens=1)
    assert prepared is not None
    assert prepared.witness["sessionId"] == prepared.session_id
    assert prepared.witness["sessionFile"] == str(session)
    assert prepared.tokens_before > 0
    assert any(
        json.loads(row).get("id") == prepared.witness["firstKeptEntryId"]
        for row in original.splitlines()
    )
    assert session.read_bytes() == original
    assert prepare_native_source(package, str(session)) is None
    assert session.read_bytes() == original


def test_canonical_owner_prepares_source_before_summary_and_commits_once(session):
    root = session.parent.parent
    registry = ThreadRegistry(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            pid=os.getpid(),
            session_file=str(session),
            goal=Goal("task", "goal"),
        )
    )
    owner, epoch = registry.live_owner_with_epoch("owner")
    owner, epoch = registry.claim_live_turn_with_epoch(owner, "turn", expected_epoch=epoch)
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    before = session.read_bytes()
    candidate = bridge.prepare_source(owner, epoch, keep_recent_tokens=1)
    assert candidate is not None
    prepared, source = candidate
    assert session.read_bytes() == before
    operation = bridge.commit(
        owner,
        epoch,
        prepared.witness,
        "Provider-free synthetic summary",
        prepared.tokens_before,
        source=source,
    )
    assert operation.status == "committed"
    assert bridge.journal.unresolved(str(session)) == ()
    assert len(bridge.journal.pending_publications(str(session))) == 1


def test_prepared_owner_source_refuses_later_bus_correction(session):
    root = session.parent.parent
    registry = ThreadRegistry(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            pid=os.getpid(),
            session_file=str(session),
            goal=Goal("task", "goal"),
        )
    )
    owner, epoch = registry.live_owner_with_epoch("owner")
    owner, epoch = registry.claim_live_turn_with_epoch(owner, "turn", expected_epoch=epoch)
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    candidate = bridge.prepare_source(owner, epoch, keep_recent_tokens=1)
    assert candidate is not None
    prepared, source = candidate
    before = session.read_bytes()
    comms = Comms(root)
    comms.register(Thread("peer", frozenset(), str(root)))
    comms.send("peer", "owner", "Retain this corrected requirement")
    with pytest.raises(RelationViolationError, match="source changed"):
        bridge.commit(
            owner,
            epoch,
            prepared.witness,
            "Stale summary",
            prepared.tokens_before,
            source=source,
        )
    assert session.read_bytes() == before
    assert bridge.journal.unresolved(str(session)) == ()


@pytest.mark.asyncio
async def test_owner_summary_discards_idle_manager_before_external_native_write(
    session, monkeypatch
):
    root = session.parent.parent
    registry = ThreadRegistry(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            pid=os.getpid(),
            session_file=str(session),
            goal=Goal("task", "goal"),
        )
    )
    owner, epoch = registry.live_owner_with_epoch("owner")
    owner, epoch = registry.claim_live_turn_with_epoch(owner, "turn", expected_epoch=epoch)
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    persistent = PersistentPiSession()
    persistent.session_file = str(session)
    persistent.session_id = json.loads(session.read_bytes().splitlines()[0])["id"]
    native_call = bridge._call

    def checked_call(*args, **kwargs):
        assert persistent.proc is None
        assert persistent.reopen_required == str(session)
        assert (
            persistent.reopen_session_id == json.loads(session.read_bytes().splitlines()[0])["id"]
        )
        return native_call(*args, **kwargs)

    monkeypatch.setattr(bridge, "_call", checked_call)

    async def synthetic_summary(metadata):
        assert metadata.session_id == persistent.session_id
        assert persistent.reopen_required is None  # preparation before retirement
        return "Synthetic provider-free summary"

    result = await compact_owner_once(
        bridge, owner, epoch, persistent, synthetic_summary, keep_recent_tokens=1
    )
    assert result is not None and result.status == "committed"
    assert persistent.reopen_required == str(session)
    assert len(bridge.journal.pending_publications(str(session))) == 1


@pytest.mark.asyncio
async def test_late_correction_after_summary_refuses_write_without_reusing_manager(session):
    root = session.parent.parent
    registry = ThreadRegistry(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            pid=os.getpid(),
            session_file=str(session),
            goal=Goal("task", "goal"),
        )
    )
    owner, epoch = registry.live_owner_with_epoch("owner")
    owner, epoch = registry.claim_live_turn_with_epoch(owner, "turn", expected_epoch=epoch)
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    persistent = PersistentPiSession()
    original = session.read_bytes()
    comms = Comms(root)
    comms.register(Thread("peer", frozenset(), str(root)))

    async def corrected_summary(metadata):
        assert metadata.tokens_before > 0
        comms.send("peer", "owner", "Correction after preparation")
        return "Now stale"

    with pytest.raises(RelationViolationError, match="source changed"):
        await compact_owner_once(
            bridge, owner, epoch, persistent, corrected_summary, keep_recent_tokens=1
        )
    assert persistent.reopen_required == str(session)
    assert session.read_bytes() == original
    assert bridge.journal.unresolved(str(session)) == ()


def test_three_sequential_native_commits_keep_exact_ids_and_prior_history(session):
    root = session.parent.parent
    registry = ThreadRegistry(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            pid=os.getpid(),
            session_file=str(session),
            goal=Goal("continuing task", "goal-unchanged"),
        )
    )
    owner, epoch = registry.live_owner_with_epoch("owner")
    owner, epoch = registry.claim_live_turn_with_epoch(owner, "turn", expected_epoch=epoch)
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    commit_ids = []
    for round_index in range(3):
        if round_index:
            # Test fixture writer, never a production owner path: append a new
            # turn using a fresh native manager after the prior commit returned.
            script = """
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {SessionManager} = await import(pathToFileURL(join(process.argv[1],
  'dist/core/session-manager.js')));
const manager=SessionManager.open(process.argv[2]);
manager.appendMessage({role:'user',content:'Correction round '+process.argv[3]+' '+
  'retain exact goal-unchanged and entry identities '.repeat(300),timestamp:5});
manager.appendMessage({role:'assistant',content:[{type:'text',text:'continued'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:6});
"""
            subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    script,
                    PACKAGE,
                    str(session),
                    str(round_index),
                ],
                capture_output=True,
                check=True,
                timeout=5,
            )
        candidate = bridge.prepare_source(owner, epoch, keep_recent_tokens=1)
        assert candidate is not None
        prepared, source = candidate
        operation = bridge.commit(
            owner,
            epoch,
            prepared.witness,
            f"Synthetic provider-free round {round_index}; retain goal-unchanged",
            prepared.tokens_before,
            source=source,
        )
        assert operation.status == "committed"
        commit_ids.append(operation.commit_id)
        assert bridge.journal.unresolved(str(session)) == ()
        assert registry.require("owner").goal.id == "goal-unchanged"
    assert len(set(commit_ids)) == 3
    assert [
        row.commit_id for row in bridge.journal.pending_publications(str(session))
    ] == commit_ids
    compactions = [
        json.loads(row)
        for row in session.read_bytes().splitlines()
        if json.loads(row).get("type") == "compaction"
    ]
    assert len(compactions) == 3
    assert [row["details"]["agentCommsCommit"]["commitId"] for row in compactions] == commit_ids


def test_invalid_session_fails_closed_without_repair(session):
    before = session.read_bytes()
    session.write_bytes(before + b'{"type":"message", broken\n')
    invalid = session.read_bytes()
    with pytest.raises(NativePreparationError):
        prepare_native_source(Path(PACKAGE), str(session), keep_recent_tokens=1)
    assert session.read_bytes() == invalid


def test_unapproved_session_alias_and_bounds_are_refused(session):
    alias = session.with_name("alias.jsonl")
    alias.symlink_to(session)
    with pytest.raises(NativePreparationError, match="canonical"):
        prepare_native_source(Path(PACKAGE), str(alias), keep_recent_tokens=1)
    with pytest.raises(NativePreparationError, match="window"):
        prepare_native_source(Path(PACKAGE), str(session), keep_recent_tokens=0)
