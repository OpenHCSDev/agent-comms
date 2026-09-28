"""Actual local pipes and durable SQLite; no Pi/provider/network calls."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agent_comms.backend import PersistentPiSession, _session_revision
from agent_comms.compaction_journal import CompactionJournal, CompactionJournalError
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_prepare import NativeWitness
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.selected_pi_child_deadline import SelectedChildUnknown
from agent_comms.selected_pi_summary_rpc import SelectedSummarySlot

CHILD = r"""
import json,sys,sqlite3,time
from pathlib import Path
mode,journal,received=sys.argv[1:]
r=json.loads(sys.stdin.readline())
# Check the actual persisted reservation before producing any result.
db=sqlite3.connect(journal)
row=db.execute('SELECT status FROM selected_summary_attempts WHERE operation_id=?',
               (r['operationId'],)).fetchone()
assert row==('reserved',),row
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
if mode=='tokens': result['tokensBefore']=True
if mode=='file': result['details']['readFiles']=['bad\0path']
if mode=='surrogate': result['summary']='\ud800'
d=dict(version=1,status='summarized',operationId=r['operationId'],
       witness=r['witness'],selected=r['selected'],settings=r['settings'],result=result)
if mode=='wrong': d['operationId']='f'*32
if mode in ('decline','limit'):
    d=dict(version=1,status='declined',operationId=r['operationId'],reason='limit_exceeded' if mode=='limit' else 'split_turn')
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
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        CHILD,
        mode,
        str(journal.path),
        str(received),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    persistent = PersistentPiSession()
    persistent.proc = child
    persistent.reader = PiRpcChannel(child.stdout)
    persistent.launch_key = ("existing-pi",)
    persistent.session_file = str(file)
    persistent.session_id = "session"
    persistent.revision = _session_revision(str(file))
    witness = NativeWitness(
        session_id="session",
        session_file=str(file),
        leaf_id="last",
        first_kept_entry_id="kept",
        revision=":".join(map(str, persistent.revision[0])),
    )
    source = dict(
        source=dict(ownerName="owner"),
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
            expected_launcher="existing-pi",
            tokens_before=1200,
            **kwargs,
        )

    try:
        yield run, persistent, journal, file, received
    finally:
        await persistent.close_idle()
        if child.returncode is None:
            child.kill()
            await child.wait()


async def test_existing_child_summary_preserves_native_metadata_and_blocks_replay(tmp_path):
    async with selected(tmp_path) as (run, persistent, journal, file, received):
        before = file.read_bytes()
        result = await run()
        assert result.summary.text == "native summary"
        assert result.summary.details == dict(readFiles=["foo.py"], modifiedFiles=[])
        assert result.summary.usage["cost"]["total"] == 0
        assert result.decline_reason is None
        assert json.loads(received.read_text())["operationId"] == result.operation_id
        assert journal.selected_summary(result.operation_id).state.declared_name == "reserved"
        assert file.read_bytes() == before
        assert not native_input_admitted(journal.path.parent, str(file))
        assert persistent.reopen_required is None
        with pytest.raises(CompactionJournalError, match="never replay"):
            await run()


async def test_decline_is_data_and_does_not_automatically_clear_input_gate(tmp_path):
    async with selected(tmp_path, "decline") as (run, _, journal, file, _received):
        result = await run()
        assert result.summary is None and result.decline_reason == "split_turn"
        assert journal.selected_summary(result.operation_id).state.declared_name == "reserved"
        assert not native_input_admitted(journal.path.parent, str(file))


@pytest.mark.parametrize(
    "mode", ["wrong", "tokens", "file", "surrogate", "duplicate", "unknown", "source", "sidecar"]
)
async def test_uncertain_result_retires_child_and_retains_unknown(tmp_path, mode):
    async with selected(tmp_path, mode) as (run, persistent, journal, file, _received):
        child = persistent.proc
        with pytest.raises(SelectedChildUnknown):
            await run()
        assert child.returncode is not None
        assert persistent.reopen_required == str(file)
        assert journal.unresolved_selected_summary(str(file))[0].state.declared_name == "unknown"
        assert not native_input_admitted(journal.path.parent, str(file))


async def test_timeout_does_not_retry_summary(tmp_path):
    async with selected(tmp_path, "hang") as (run, persistent, journal, file, received):
        with pytest.raises(SelectedChildUnknown, match="made no progress for 0.15 seconds"):
            await run(idle_timeout_seconds=0.15)
        assert persistent.proc is None and received.exists()
        assert len(journal.unresolved_selected_summary(str(file))) == 1


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
        assert persistent.proc is None
        operation = json.loads(received.read_text())["operationId"]
        assert journal.selected_summary(operation).state.declared_name == "unknown"
        assert not native_input_admitted(journal.path.parent, str(file))
        assert len(journal.unresolved_selected_summary(str(file))) == 1


async def test_cancellation_retains_durable_unknown_and_reaps(tmp_path):
    async with selected(tmp_path, "hang") as (run, persistent, journal, file, received):
        child = persistent.proc
        task = asyncio.create_task(run())
        async with asyncio.timeout(2):
            while not received.exists():
                await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.returncode is not None
        assert journal.unresolved_selected_summary(str(file))[0].state.declared_name == "unknown"


async def test_stale_child_never_reserves_or_sends(tmp_path):
    async with selected(tmp_path) as (run, persistent, journal, file, received):
        persistent.revision = None
        with pytest.raises(SelectedChildUnknown, match="stale"):
            await run()
        assert not received.exists()
        assert not journal.unresolved_selected_summary(str(file))


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


@pytest.mark.skipif(not os.environ.get("PI_NATIVE_PACKAGE_DIR"), reason="Owned copied Pi fixture")
@pytest.mark.parametrize("provider_error", [False, True])
async def test_python_to_actual_native_rpc_retains_summary_without_native_write(
    tmp_path, provider_error
):
    env = dict(os.environ, PR95_RPC_FIXTURE="1", TMPDIR=str(tmp_path))
    if provider_error:
        env["PR95_PROVIDER_ERROR"] = "1"
    script = (
        Path(__file__).resolve().parents[1] / "stack/test-native-selected-compaction-summary.mjs"
    )
    child = await asyncio.create_subprocess_exec(
        "node",
        str(script),
        env=env,
        start_new_session=True,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    persistent = PersistentPiSession()
    persistent.proc = child
    try:
        async with asyncio.timeout(10):
            line = await child.stderr.readline()
            assert line.startswith(b"{"), line.decode()
            fixture = json.loads(line)
            persistent.reader = PiRpcChannel(child.stdout)
            persistent.session_file = fixture["sessionFile"]
            persistent.session_id = fixture["witness"]["sessionId"]
            persistent.revision = _session_revision(fixture["sessionFile"])
            persistent.launch_key = ("native-fixture",)
            source = dict(
                source=dict(ownerName="owner"),
                selected=fixture["selected"],
                settings=fixture["settings"],
            )
            file = Path(fixture["sessionFile"])
            before = file.read_bytes()
            journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
            exchange = SelectedSummarySlot("owner", persistent.session_id).run_selected_summary(
                persistent,
                journal,
                FieldCodec.decode(NativeWitness, fixture["witness"]),
                source,
                tokens_before=fixture["tokensBefore"],
                expected_launcher="native-fixture",
                idle_timeout_seconds=5,
            )
            if provider_error:
                with pytest.raises(
                    SelectedChildUnknown, match="402: insufficient credits on configured model"
                ):
                    await exchange
                assert file.read_bytes() == before
                assert len(journal.unresolved_selected_summary(str(file))) == 1
                assert (
                    journal.unresolved_selected_summary(str(file))[0].state.declared_name
                    == "unknown"
                )
                assert not native_input_admitted(journal.path.parent, str(file))
                assert persistent.proc is None
                return
            result = await exchange
            assert "Synthetic summary" in result.summary.text
            assert result.summary.details == dict(readFiles=[], modifiedFiles=[])
            assert result.summary.usage["output"] > 0
            assert file.read_bytes() == before
            assert journal.selected_summary(result.operation_id).state.declared_name == "reserved"
            assert not native_input_admitted(journal.path.parent, str(file))
            child.stdin.close()
            assert await child.wait() == 0
    finally:
        await persistent.close_idle()


async def test_limit_decline_is_durable_and_never_admits_original(tmp_path):
    from agent_comms.cli import main
    from agent_comms.comms import wire
    from agent_comms.threads import Thread

    async with selected(tmp_path, "limit") as (run, persistent, journal, file, received):
        result = await run()
        assert result.summary is None and result.decline_reason == "limit_exceeded"
        attempt = CompactionJournal(journal.path).selected_summary(result.operation_id)
        assert attempt.state.declared_name == "refused"
        assert attempt.state.decline_reason == "limit_exceeded"
        assert not attempt.state.original_eligible
        assert persistent.proc.returncode is None
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
        assert persistent.proc.returncode is None


async def test_repeated_progress_does_not_hide_stalled_provider(tmp_path):
    async with selected(tmp_path, "duplicate-progress") as (run, persistent, journal, file, _):
        with pytest.raises(SelectedChildUnknown, match="made no progress"):
            await run(idle_timeout_seconds=0.06)
        assert journal.unresolved_selected_summary(str(file))[0].state.declared_name == "unknown"
        assert persistent.proc is None


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
            await run(idle_timeout_seconds=.06)
        assert journal.unresolved_selected_summary(str(file))[0].state.declared_name == "unknown"
        assert persistent.proc is None
