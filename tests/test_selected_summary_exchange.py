"""Actual local pipes and durable SQLite; no Pi/provider/network calls."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from agent_comms.child_process import AttachedChild
from agent_comms.compaction_errors import CompactionJournalError
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.owner_compaction_prepare import NativeWitness
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.selected_pi_summary_rpc import SelectedChildUnknown, SelectedSummarySlot
from retained_native_fixture import retained_native_host
from selected_summary_cases import manual_source

CHILD = r"""
import json,sys,sqlite3,time
from agent_comms.compaction_journal import SelectedSummaryAttempt
from agent_comms.compaction_states import ReservedSummary
from pathlib import Path
mode,journal,received=sys.argv[1:]
print('ready',file=sys.stderr,flush=True)
r=json.loads(sys.stdin.readline())
# Check the actual persisted reservation before producing any result.
db=sqlite3.connect(journal)
row=SelectedSummaryAttempt.one(db,operation_id=r['operationId'])
assert row is not None and isinstance(row.state,ReservedSummary),row
Path(received).write_text(json.dumps(r))
if mode in ('progress','duplicate-progress','foreign-progress'):
    for sequence in range(1, 6):
        print(json.dumps(dict(type='agent_comms_compaction_progress',id=r['id'],
              operationId='foreign' if mode=='foreign-progress' else r['operationId'],
              sequence=sequence if mode=='progress' else 1)),flush=True)
        time.sleep(.035)
if mode=='hang':
    time.sleep(30)
if mode=='source':
    with open(r['witness']['sessionFile'],'a') as f: f.write('{}\n')
if mode=='sidecar':
    Path(r['witness']['sessionFile']+'.input-proof').write_text('{}\n')
if mode=='large':
    print('x'*200000,flush=True)
    time.sleep(30)
cost=dict.fromkeys(['input','output','cacheRead','cacheWrite','total'],0)
usage=dict.fromkeys(['input','output','cacheRead','cacheWrite','totalTokens'],0)
usage['cost']=cost
result=dict(summary='native summary',firstKeptEntryId=r['witness']['firstKeptEntryId'],
            tokensBefore=1200,details=dict(readFiles=['foo.py'],modifiedFiles=[]),usage=usage)
if mode=='many-files':
    result['details']['readFiles']=[f'{n}/'+'x'*3990 for n in range(3800)]
if mode=='tokens': result['tokensBefore']=True
if mode=='file': result['details']['readFiles']=['bad\0path']
if mode=='surrogate': result['summary']='\ud800'
d=dict(version=1,status='summarized',operationId=r['operationId'],
       witness=r['witness'],selected=r['selected'],settings=r['settings'],result=result)
if mode=='wrong': d['operationId']='f'*32
if mode in ('decline','limit'):
    d=dict(version=1,status='declined',operationId=r['operationId'],
           reason='limit_exceeded' if mode=='limit' else 'split_turn')
if mode=='unknown': d=dict(version=1,status='unknown',operationId=r['operationId'])
if mode in ('provider-error', 'invalid-error', 'oversize-error'):
    d=dict(version=1,status='unknown',operationId=r['operationId'],
           reason='402: insufficient credits on configured model')
    if mode=='invalid-error': d['reason']='bad\x1bdetail'
    if mode=='oversize-error': d['reason']='x'*1025
response=dict(id=r['id'],type='response',command=r['type'],success=True,data=d)
raw=json.dumps(response)
if mode=='duplicate': raw=raw[:-1]+',"success":true}'
print(raw,flush=True)
# Behave as an existing idle child: do not exit just after our one exchange.
sys.stdin.read()
"""


@asynccontextmanager
async def selected(tmp_path, mode="success"):
    file = tmp_path / "session.jsonl"
    file.write_text('{"type":"session","version":3,"id":"session"}\n')
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    received = tmp_path / "received.json"
    child = await AttachedChild.start(
        (
            sys.executable,
            "-u",
            "-c",
            CHILD,
            mode,
            str(journal.path),
            str(received),
        )
    )
    async with asyncio.timeout(5):
        assert await child.stderr.readline() == b"ready\n"
    persistent = retained_native_host(
        child,
        NativePiRpcLaunch((sys.executable,), tmp_path, {}, tmp_path, file, tmp_path),
        NativeSessionIdentity("session", str(file)),
    )
    witness = NativeWitness(
        session_id="session",
        session_file=str(file),
        leaf_id="last",
        first_kept_entry_id="kept",
        revision=":".join(map(str, persistent.custody.revision[0])),
    )
    source = dict(
        source=manual_source(persistent.custody.identity.session_file),
        selected=dict(provider="fixture", modelId="fixture", contextWindow=4096),
        settings=dict(reserveTokens=100, keepRecentTokens=100),
    )
    slot = SelectedSummarySlot("owner", "session")

    async def run(**kwargs):
        return await slot.run_selected_summary(
            persistent,
            journal,
            witness,
            source,
            expected_package=tmp_path,
            tokens_before=1200,
            **kwargs,
        )

    try:
        yield run, persistent, journal, file, received
    finally:
        await persistent.close_idle()


async def test_existing_child_summary_preserves_native_metadata_and_blocks_replay(tmp_path):
    async with selected(tmp_path) as (run, persistent, journal, file, received):
        before = file.read_bytes()
        result = await run()
        assert result.summary.text == "native summary"
        assert result.summary.details.read_files == ("foo.py",)
        assert result.summary.usage.cost.total == 0
        assert result.decline_reason is None
        assert json.loads(received.read_text())["operationId"] == result.operation_id
        assert journal.summaries.get(result.operation_id).state.declared_name == "reserved"
        assert file.read_bytes() == before
        assert not native_input_admitted(journal.path.parent, str(file))
        assert persistent.custody.idle().current
        with pytest.raises(CompactionJournalError, match="never replay"):
            await run()


async def test_decline_is_data_and_does_not_automatically_clear_input_gate(tmp_path):
    async with selected(tmp_path, "decline") as (run, _, journal, file, _received):
        result = await run()
        assert result.summary is None and result.decline_reason == "split_turn"
        assert journal.summaries.get(result.operation_id).state.declared_name == "reserved"
        assert not native_input_admitted(journal.path.parent, str(file))


@pytest.mark.parametrize(
    "mode", ["wrong", "tokens", "file", "surrogate", "duplicate", "unknown", "source", "sidecar"]
)
async def test_uncertain_result_retires_child_and_retains_unknown(tmp_path, mode):
    async with selected(tmp_path, mode) as (run, persistent, journal, file, _received):
        child = persistent.custody.child.proc
        with pytest.raises(SelectedChildUnknown):
            await run()
        assert child.returncode is not None
        assert persistent.custody.session_file == str(file)
        assert journal.summaries.unresolved(str(file))[0].state.declared_name == "unknown"
        assert not native_input_admitted(journal.path.parent, str(file))


async def test_timeout_does_not_retry_summary(tmp_path):
    async with selected(tmp_path, "hang") as (run, persistent, journal, file, received):
        with pytest.raises(SelectedChildUnknown, match="made no progress for 0.15 seconds"):
            await run(idle_timeout_seconds=0.15)
        assert not persistent.available and received.exists()
        assert len(journal.summaries.unresolved(str(file))) == 1


@pytest.mark.parametrize(
    ("mode", "detail"),
    [
        ("provider-error", "402: insufficient credits on configured model"),
        ("invalid-error", "Invalid selected summary failure detail"),
        ("oversize-error", "Invalid selected summary failure detail"),
    ],
)
async def test_failure_detail_survives_without_authorizing_replay(tmp_path, mode, detail):
    async with selected(tmp_path, mode) as (run, persistent, journal, file, received):
        with pytest.raises(SelectedChildUnknown, match=detail):
            await run()
        assert not persistent.available
        operation = json.loads(received.read_text())["operationId"]
        assert journal.summaries.get(operation).state.declared_name == "unknown"
        assert not native_input_admitted(journal.path.parent, str(file))
        assert len(journal.summaries.unresolved(str(file))) == 1


async def test_cancellation_retains_durable_unknown_and_reaps(tmp_path):
    async with selected(tmp_path, "hang") as (run, persistent, journal, file, received):
        child = persistent.custody.child.proc
        task = asyncio.create_task(run())
        async with asyncio.timeout(2):
            while not received.exists():
                await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.returncode is not None
        assert journal.summaries.unresolved(str(file))[0].state.declared_name == "unknown"


@pytest.mark.parametrize("changed", ["revision", "package"])
async def test_stale_child_never_reserves_or_sends(tmp_path, changed):
    async with selected(tmp_path) as (run, persistent, journal, file, received):
        if changed == "revision":
            persistent.custody.revision = ((0, 0, 0, 0, 0), None)
        else:
            launch, auth = persistent.custody.child.key
            persistent.custody.child.key = (
                replace(launch, package=tmp_path / "changed-package"),
                auth,
            )
        with pytest.raises(SelectedChildUnknown, match="stale"):
            await run()
        assert not received.exists()
        assert not journal.summaries.unresolved(str(file))


async def test_bounded_reader_refuses_oversize_across_transport_fragments():
    stream = asyncio.StreamReader(limit=16)
    reader = PiRpcChannel(stream)
    stream.feed_data(b"x" * 40)
    with pytest.raises(ValueError, match="transport limit"):
        await reader.readline(max_bytes=32)
    assert reader.chunks == []


async def test_reader_keeps_partial_record_on_cancel_and_enforces_bound():
    stream = asyncio.StreamReader(limit=16)
    reader = PiRpcChannel(stream)
    stream.feed_data(b"x" * 20)
    task = asyncio.create_task(reader.readline(max_bytes=32))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.chunks == [b"x" * 20]
    stream.feed_data(b"y" * 13 + b"\n")
    with pytest.raises(ValueError, match="transport limit"):
        await reader.readline(max_bytes=32)


async def test_limit_decline_is_durable_and_never_admits_original(tmp_path):
    from agent_comms.cli import main
    from agent_comms.comms import wire
    from agent_comms.threads import Thread

    async with selected(tmp_path, "limit") as (run, persistent, journal, file, received):
        result = await run()
        assert result.summary is None and result.decline_reason == "limit_exceeded"
        attempt = CompactionJournal(journal.path).summaries.get(result.operation_id)
        assert attempt.state.declared_name == "refused"
        assert attempt.state.decline_reason == "limit_exceeded"
        assert not attempt.state.original_eligible
        assert persistent.custody.child.proc.returncode is None
        assert not native_input_admitted(tmp_path, str(file))
        comms = wire(tmp_path)
        comms.registry.register(Thread("owner", frozenset(), str(tmp_path), session_file=str(file)))
        assert main(["--root", str(tmp_path), "compaction-status", "--thread", "owner"]) == 0
        before = received.read_bytes()
        with pytest.raises(CompactionJournalError, match="never replay"):
            await run()
        assert received.read_bytes() == before


async def test_observable_progress_extends_idle_deadline_without_total_limit(tmp_path):
    async with selected(tmp_path, "progress") as (run, persistent, journal, file, _):
        result = await run(idle_timeout_seconds=0.08)
        assert result.summary.text == "native summary"
        assert persistent.custody.child.proc.returncode is None


async def test_repeated_progress_does_not_hide_stalled_provider(tmp_path):
    async with selected(tmp_path, "duplicate-progress") as (run, persistent, journal, file, _):
        with pytest.raises(SelectedChildUnknown, match="made no progress"):
            await run(idle_timeout_seconds=0.06)
        assert journal.summaries.unresolved(str(file))[0].state.declared_name == "unknown"
        assert not persistent.available


async def test_manual_instructions_share_selected_rpc_and_adaptive_omits_field(tmp_path):
    async with selected(tmp_path, "decline") as (run, _, journal, file, received):
        await run(custom_instructions="Preserve the owner decisions")
        assert (
            json.loads(received.read_text())["customInstructions"] == "Preserve the owner decisions"
        )
    other = tmp_path / "adaptive"
    other.mkdir()
    async with selected(other, "decline") as (run, _, journal, file, received):
        await run()
        assert "customInstructions" not in json.loads(received.read_text())


async def test_foreign_progress_cannot_extend_selected_attempt(tmp_path):
    async with selected(tmp_path, "foreign-progress") as (run, persistent, journal, file, _):
        with pytest.raises(SelectedChildUnknown, match="Foreign selected compaction progress"):
            await run(idle_timeout_seconds=0.06)
        assert journal.summaries.unresolved(str(file))[0].state.declared_name == "unknown"
        assert not persistent.available


async def test_selected_frame_uses_transport_without_retired_file_count_budget(tmp_path):
    async with selected(tmp_path, "many-files") as (run, persistent, journal, file, _):
        result = await run()
        assert len(result.summary.details.read_files) == 3800
        assert persistent.custody.child.proc.returncode is None
        assert journal.summaries.blocking(str(file))


async def test_refusal_recovery_rechecks_exact_record_and_never_reattempts(tmp_path):
    from agent_comms.compaction_states import RetiredRefusalSummary

    async with selected(tmp_path, "limit") as (run, _, journal, file, _):
        result = await run()
        attempt = journal.summaries.get(result.operation_id)
        journal.summaries.refuse(result.operation_id, result.decline_reason)
        assert journal.summaries.get(result.operation_id) == attempt
        with pytest.raises(CompactionJournalError, match="refusal transition"):
            journal.summaries.refuse(result.operation_id, "different native reason")
        with pytest.raises(CompactionJournalError, match="commit reservation"):
            attempt.state.require_commit_reservation()
        journal.summaries.retire_refused(attempt)
        retired = journal.summaries.get(result.operation_id)
        assert isinstance(retired.state, RetiredRefusalSummary)
        assert retired.state.decline_reason == result.decline_reason
        with pytest.raises(CompactionJournalError, match="changed"):
            journal.summaries.retire_refused(attempt)
        assert journal.summaries.get(result.operation_id) == retired


@pytest.mark.parametrize(
    "mismatch", [None, "operation", "witness", "model", "settings", "missing", "reason"]
)
def test_failed_receipt_requires_exact_attestation(mismatch):
    from agent_comms.owner_compaction_settings import PiCompactionSettings
    from agent_comms.pi_commands import AgentCommsSummarizeCompaction
    from agent_comms.pi_summary_payloads import SelectedModel, SummaryFailedData
    from agent_comms.selected_pi_summary_rpc import SelectedSummaryFailed, _summary_response

    request = AgentCommsSummarizeCompaction(
        id="selected",
        version=1,
        operation_id="a" * 32,
        witness=NativeWitness("session", "/saved.jsonl", "leaf", "kept", "1:2:3:4:5"),
        selected=SelectedModel("fixture", "fixture", 32768),
        settings=PiCompactionSettings(2048, 1024),
    )
    data = request.to_rpc()
    data.pop("id")
    data.pop("type")
    data.update(status="failed", reason="Provider rejected the summary")
    if mismatch == "operation":
        data["operationId"] = "b" * 32
    elif mismatch == "witness":
        data["witness"]["leafId"] = "different"
    elif mismatch == "model":
        data["selected"]["modelId"] = "other"
    elif mismatch == "settings":
        data["settings"]["reserveTokens"] += 1
    elif mismatch == "missing":
        del data["witness"]
    elif mismatch == "reason":
        data["reason"] = "control\x1bcharacter"
    raw = (
        json.dumps(
            dict(
                type="response",
                id=request.id,
                command=request.declared_name,
                success=True,
                data=data,
            )
        )
        + "\n"
    ).encode()
    if mismatch is not None:
        with pytest.raises(SelectedChildUnknown):
            _summary_response(raw, request, 1200)
    else:
        receipt = _summary_response(raw, request, 1200)
        assert isinstance(receipt, SummaryFailedData)
        error = SelectedSummaryFailed(receipt)
        assert error.operation_id == request.operation_id
        assert error.reason == data["reason"]
