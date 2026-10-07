"""Actual retained native summaries and strict RPC boundary contracts.

Only provider responses are controlled locally. Native saved state, transport,
reservation, cancellation and child custody are real; no substitute child host.
"""


from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.retained_task_facts import RetainedTaskFacts
from agent_comms.input_disposition import InputDispositions
from agent_comms.compaction_errors import CompactionJournalError
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import SelectedSummarySource
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.pi_vocabulary import ManualCompactionReason
from agent_comms.selected_source import SessionRevision
from agent_comms.selected_pi_summary_rpc import SelectedChildUnknown, SelectedSummarySlot

pytest_plugins = ("test_backend_native_lifecycle",)


@pytest.fixture
async def retained_summary(native_backend):
    """Prepare a real SDK source without replacing the retained child or its reader."""
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.owner_compaction_prepare import prepare_native_source
    from agent_comms.owner_compaction_settings import PiCompactionSettings
    from agent_comms.pi_summary_payloads import SelectedModel
    from agent_comms.selected_source import ManualSource
    from agent_comms.thread_identity import TurnId
    from agent_comms.threads import Thread

    native = native_backend
    settings = PiCompactionSettings(2048, 1)
    configured = json.loads((native.config / "settings.json").read_text())
    configured["compaction"].update(reserveTokens=2048, keepRecentTokens=1)
    (native.config / "settings.json").write_text(json.dumps(configured))
    for text in ("First original retained question", "Second original retained question"):
        assert (await native.run(text))[-1].ok
    model = native.persistent.custody.child.attestation.state.model
    assert model is not None
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]).resolve(strict=True)
    preparation = (await asyncio.to_thread(
        prepare_native_source,
        package,
        str(native.session),
        settings=settings,
        context_window=model.context_window,
    )).require_ready()

    comms = Comms(native.root)
    comms.registry.declare(
        Thread(
            "owner",
            frozenset(),
            str(native.project),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(native.session),
            model=model.display_name,
        )
    )
    admitted = comms.agents.begin_turn("owner", "native-summary-negative")
    lease, owner = admitted.turn_lease, admitted.thread
    envelope = SelectedSummarySource(
        source=ManualSource(
            owner=owner.require_process(),
            incarnation=owner.incarnation,
            turn=TurnId(lease.turn_id),
            reserved_revision=SessionRevision.observe(str(native.session)).require_available(),
        ),
        selected=SelectedModel(model.provider, model.id, model.context_window),
        settings=settings,
        retained=RetainedTaskFacts(()),
    )
    journal = CompactionJournal(native.root / "compaction-commits.sqlite3")
    slot = SelectedSummarySlot("owner", preparation.witness.session_id)

    async def run(*, source=envelope, expected_package=package, **options):
        return await slot.run_selected_summary(
            native.persistent,
            journal,
            preparation.witness,
            source,
            owner=owner,
            expected_package=expected_package,
            tokens_before=preparation.tokens_before,
            reason=ManualCompactionReason,
            **options,
        )

    try:
        yield native, preparation, envelope, journal, run
    finally:
        comms.agents.finish_turn(lease)


@pytest.mark.parametrize("changed", ["package", "revision", "sidecar"])
async def test_actual_retained_stale_source_never_reserves_or_sends(retained_summary, changed):
    native, _, _, journal, run = retained_summary
    original, inputs, calls = (
        native.session.read_bytes(),
        native.saved_inputs(),
        native.provider.posts,
    )
    options = {}
    if changed == "package":
        options["expected_package"] = native.project
    else:
        path = (
            native.session if changed == "revision" else Path(str(native.session) + ".input-proof")
        )
        assert path.exists()
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    with pytest.raises(SelectedChildUnknown, match="stale"):
        await run(**options)
    assert not journal.summaries.history(str(native.session))
    assert native.provider.posts == calls
    assert native.session.read_bytes() == original
    assert native.saved_inputs() == inputs


@pytest.mark.parametrize("changed", ["model", "settings"])
async def test_actual_selected_mismatch_refuses_without_provider_or_replay(
    retained_summary, changed
):
    from agent_comms.compaction_records import SelectedSummarySource
    from agent_comms.compaction_states import RetiredRefusalSummary
    from agent_comms.field_codec import FieldCodec
    from agent_comms.pi_summary_payloads import SummaryDeclinedData

    native, _, envelope, journal, run = retained_summary
    original, inputs, calls = (
        native.session.read_bytes(),
        native.saved_inputs(),
        native.provider.posts,
    )
    source = envelope
    source = (
        replace(source, selected=replace(source.selected, model_id="unselected-model"))
        if changed == "model"
        else replace(source, settings=replace(source.settings, reserve_tokens=2049))
    )
    result = await run(source=source)
    assert isinstance(result, SummaryDeclinedData)
    assert result.reason == changed + "_mismatch"
    attempt = journal.summaries.get(result.operation_id)
    assert attempt.state.declared_name == "refused"
    assert journal.summaries.blocking(str(native.session)) == (attempt,)
    assert not native_input_admitted(native.root, str(native.session))
    assert native.persistent.custody.idle().current
    assert native.provider.posts == calls
    assert native.session.read_bytes() == original
    assert native.saved_inputs() == inputs
    with pytest.raises(CompactionJournalError, match="never replay"):
        await run(source=source)
    assert native.provider.posts == calls
    journal.summaries.refuse(result.operation_id, result.reason)
    assert journal.summaries.get(result.operation_id) == attempt
    with pytest.raises(CompactionJournalError, match="refusal transition"):
        journal.summaries.refuse(result.operation_id, "different native reason")
    with pytest.raises(CompactionJournalError, match="commit reservation"):
        attempt.state.require_commit_reservation()
    check = attempt.request.reservation_check(
        SessionRevision.observe(str(native.session)),
        InputDispositions(native.root / "input_dispositions.json").read(),
    )
    journal.summaries.retire_unchanged(attempt, check)
    retired = journal.summaries.get(result.operation_id)
    assert isinstance(retired.state, RetiredRefusalSummary)
    assert retired.state.decline_reason == result.reason
    with pytest.raises(CompactionJournalError, match="changed"):
        journal.summaries.retire_unchanged(attempt, check)
    assert journal.summaries.get(result.operation_id) == retired
    assert native.provider.posts == calls
    assert native.session.read_bytes() == original


@pytest.mark.parametrize("termination", ["timeout", "cancel", "disconnect"])
async def test_actual_inflight_summary_uncertainty_reaps_and_never_replays(
    retained_summary, termination
):
    native, _, _, journal, run = retained_summary
    original, inputs, calls = (
        native.session.read_bytes(),
        native.saved_inputs(),
        native.provider.posts,
    )
    native.provider.status = 0
    child = native.persistent.custody.child.proc
    task = asyncio.create_task(run(idle_timeout_seconds=0.5 if termination == "timeout" else 10))
    try:
        async with asyncio.timeout(5):
            while native.provider.posts == calls:
                assert not task.done(), task.result() if task.done() else None
                await asyncio.sleep(0.005)
        if termination == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if termination == "disconnect":
                await child.stop()
            with pytest.raises(SelectedChildUnknown):
                await task
        assert not child.alive()
        assert not native.persistent.available
        (attempt,) = journal.summaries.history(str(native.session))
        assert attempt.state.declared_name == "unknown"
        assert journal.summaries.blocking(str(native.session)) == (attempt,)
        assert not native_input_admitted(native.root, str(native.session))
        assert native.provider.posts == calls + 1
        assert native.session.read_bytes() == original
        assert native.saved_inputs() == inputs
        with pytest.raises(SelectedChildUnknown):
            await run()
        assert native.provider.posts == calls + 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_actual_joined_provider_failure_preserves_source_without_replay(retained_summary):
    from agent_comms.selected_pi_summary_rpc import SelectedSummaryFailed

    native, _, _, journal, run = retained_summary
    original, inputs, calls = (
        native.session.read_bytes(),
        native.saved_inputs(),
        native.provider.posts,
    )
    native.provider.status = 400
    with pytest.raises(SelectedSummaryFailed, match="maximum context length exceeded") as failure:
        await run()
    attempt = journal.summaries.get(failure.value.operation_id)
    assert attempt.state.declared_name == "failed"
    assert attempt.state.terminal and attempt.state.settled_without_original
    assert not attempt.state.original_eligible
    assert not journal.summaries.blocking(str(native.session))
    assert native.persistent.custody.idle().current
    assert native.provider.posts == calls + 1
    assert native.session.read_bytes() == original
    assert native.saved_inputs() == inputs


async def test_actual_reservation_cancellation_joins_before_releasing_native_custody(
    retained_summary, monkeypatch,
):
    import threading
    from agent_comms.store_files import _store_lock

    native, _, _, journal, run = retained_summary
    original, calls = native.session.read_bytes(), native.provider.posts
    entered = threading.Event()
    reserve = journal.summaries.reserve

    def observe(*args, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        return reserve(*args, **kwargs)

    monkeypatch.setattr(journal.summaries, "reserve", observe)
    with _store_lock(native.root / "wire"):
        task = asyncio.create_task(run())
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "original reservation worker must join before custody exits"
    with pytest.raises(asyncio.CancelledError):
        await task
    attempts = journal.summaries.history(str(native.session))
    assert len(attempts) == 1 and attempts[0].state.declared_name == "reserved"
    assert journal.summaries.blocking(str(native.session))
    assert native.provider.posts == calls and native.session.read_bytes() == original
    assert native.persistent.custody.idle().current
    print("actual_reservation_cancel_joined_no_summary_send", attempts[0].operation_id)


async def test_actual_failure_settlement_cancel_preserves_known_native_result(
    retained_summary, monkeypatch,
):
    import threading

    native, preparation, _, journal, run = retained_summary
    original, calls = native.session.read_bytes(), native.provider.posts
    entered, release = threading.Event(), threading.Event()
    fail = journal.summaries.fail

    def observe(*args):
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        assert release.wait(10)
        return fail(*args)

    monkeypatch.setattr(journal.summaries, "fail", observe)
    native.provider.status = 400
    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(entered.wait, 15)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    attempts = journal.summaries.history(str(native.session))
    assert len(attempts) == 1 and attempts[0].state.declared_name == "failed"
    assert attempts[0].state.terminal and attempts[0].state.settled_without_original
    assert not attempts[0].state.original_eligible
    # This actual SDK cut splits the second native turn. The original compact
    # owner concurrently requests history and turn-prefix summaries; they are
    # distinct sources, not a replay of one provider request.
    assert preparation.is_split_turn
    requests = native.provider.requests[calls:]
    assert len(requests) == 2 and requests[0]["messages"] != requests[1]["messages"]
    assert native.provider.posts == calls + 2 and native.session.read_bytes() == original
    assert native.persistent.custody.idle().current
    print("actual_known_failure_settled_despite_cancel", attempts[0].operation_id)


async def test_actual_native_progress_extends_idle_deadline_without_total_cap(retained_summary):
    native, _, _, journal, run = retained_summary
    original, inputs = native.session.read_bytes(), native.saved_inputs()
    events = []
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def slow_consumer(event):
        events.append(event)
        await asyncio.sleep(0.2)

    result = await run(idle_timeout_seconds=0.3, on_event=slow_consumer)
    assert len(events) >= 2
    assert loop.time() - started > 0.3
    assert all(event.operation_id == result.operation_id for event in events)
    assert journal.summaries.get(result.operation_id).state.declared_name == "reserved"
    assert not native_input_admitted(native.root, str(native.session))
    assert native.persistent.custody.idle().current
    assert native.session.read_bytes() == original
    assert native.saved_inputs() == inputs


@pytest.mark.parametrize("fault", ["foreign", "repeated"])
async def test_actual_native_pipe_progress_fault_never_authorizes_replay(
    retained_summary, monkeypatch, fault
):
    """Inject a wire fault into the real child's pipe; never replace its reader.

    The observer delegates every decode unchanged. Repeated progress copies the
    actual native frame; the foreign frame changes its correlation identity.
    The provider remains in flight, the native source and journal remain real.
    """
    from agent_comms.pi_events import AgentCommsCompactionProgress

    native, _, _, journal, run = retained_summary
    original, inputs, calls = (
        native.session.read_bytes(),
        native.saved_inputs(),
        native.provider.posts,
    )
    native.provider.status = 0
    child = native.persistent.custody.child.proc
    progress = asyncio.get_running_loop().create_future()
    decode = PiRpcChannel.decode_record

    def observe(raw, **options):
        event = decode(raw, **options)
        if isinstance(event, AgentCommsCompactionProgress) and not progress.done():
            progress.set_result((raw, event))
        return event

    monkeypatch.setattr(PiRpcChannel, "decode_record", staticmethod(observe))
    task = asyncio.create_task(run(idle_timeout_seconds=0.8))
    try:
        raw, event = await asyncio.wait_for(progress, 5)
        async with asyncio.timeout(5):
            while native.provider.posts == calls:
                assert not task.done(), task.result() if task.done() else None
                await asyncio.sleep(0.005)
        if fault == "foreign":
            raw = (json.dumps(replace(event, id="foreign").to_wire()) + "\n").encode()
        # No helper executable or mock transport: an additional kernel writer
        # targets this fixture child's existing stdout pipe only.
        async with asyncio.timeout(3):
            while not task.done():
                try:
                    with open(f"/proc/{child.pid}/fd/1", "wb", buffering=0) as output:
                        output.write(raw)
                except (BrokenPipeError, FileNotFoundError):
                    assert not child.alive()
                    break
                await asyncio.sleep(0.1)
        detail = (
            "Foreign selected compaction progress" if fault == "foreign" else "made no progress"
        )
        with pytest.raises(SelectedChildUnknown, match=detail):
            await task
        assert not child.alive() and not native.persistent.available
        (attempt,) = journal.summaries.history(str(native.session))
        assert attempt.state.declared_name == "unknown"
        assert not native_input_admitted(native.root, str(native.session))
        assert native.provider.posts == calls + 1
        assert native.session.read_bytes() == original
        assert native.saved_inputs() == inputs
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def selected_request():
    from agent_comms.owner_compaction_prepare import NativeWitness
    from agent_comms.owner_compaction_settings import PiCompactionSettings
    from agent_comms.pi_commands import AgentCommsSummarizeCompaction
    from agent_comms.pi_summary_payloads import SelectedModel

    return AgentCommsSummarizeCompaction(
        id="selected",
        version=1,
        operation_id="a" * 32,
        witness=NativeWitness("session", "/saved.jsonl", "leaf", "kept", "1:2:3:4:5"),
        selected=SelectedModel("fixture", "fixture", 32768),
        settings=PiCompactionSettings(2048, 1024),
        retained_text=RetainedTaskFacts(()).compaction_text,
    )


def summarized_frame(request):
    """External wire contract only: no process, journal or retained state facade."""
    data = request.to_rpc()
    data.pop("id")
    data.pop("type")
    data.pop("retainedText")
    data.update(
        status="summarized",
        result=dict(
            summary=request.retained_text + "\n\nnative summary",
            firstKeptEntryId="kept",
            tokensBefore=1200,
            details=dict(readFiles=["foo.py"], modifiedFiles=[]),
            usage=dict(
                input=1,
                output=1,
                cacheRead=0,
                cacheWrite=0,
                totalTokens=2,
                cost=dict(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
            ),
        ),
    )
    return dict(
        type="response", id=request.id, command=request.declared_name, success=True, data=data
    )


@pytest.mark.parametrize(
    "path,value",
    [
        (("id",), "unrelated"),
        (("success",), False),
        (("data", "operationId"), "b" * 32),
        (("data", "witness", "leafId"), "different"),
        (("data", "selected", "modelId"), "different"),
        (("data", "settings", "reserveTokens"), 2049),
        (("data", "result", "tokensBefore"), True),
        (("data", "result", "tokensBefore"), 1201),
        (("data", "result", "firstKeptEntryId"), "different"),
        (("data", "result", "summary"), "\ud800"),
        (("data", "result", "details", "readFiles"), ["bad\0path"]),
    ],
)
def test_summary_wire_refuses_uncorrelated_or_invalid_native_data(path, value):
    from agent_comms.selected_pi_summary_rpc import _summary_response

    request = selected_request()
    frame = summarized_frame(request)
    target = frame
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(SelectedChildUnknown):
        _summary_response((json.dumps(frame) + "\n").encode(), request, 1200)


@pytest.mark.parametrize("corruption", ["duplicate", "incomplete"])
def test_summary_wire_requires_one_complete_unambiguous_record(corruption):
    from agent_comms.selected_pi_summary_rpc import _summary_response

    request = selected_request()
    raw = json.dumps(summarized_frame(request))
    raw = raw[:-1] + ',"success":true}\n' if corruption == "duplicate" else raw
    with pytest.raises(SelectedChildUnknown):
        _summary_response(raw.encode(), request, 1200)


@pytest.mark.parametrize(
    "reason,detail",
    [
        (None, "no failure detail"),
        ("402: insufficient credits on configured model", "402: insufficient credits"),
        ("bad\x1bdetail", "Invalid selected summary failure detail"),
        ("x" * 1025, "Invalid selected summary failure detail"),
    ],
)
def test_unknown_summary_wire_preserves_only_valid_provider_detail(reason, detail):
    from agent_comms.selected_pi_summary_rpc import _summary_response

    request = selected_request()
    frame = summarized_frame(request)
    frame["data"] = dict(
        version=1, status="unknown", operationId=request.operation_id, reason=reason
    )
    with pytest.raises(SelectedChildUnknown, match=detail):
        _summary_response((json.dumps(frame) + "\n").encode(), request, 1200)


def test_summary_file_metadata_uses_transport_without_retired_count_or_total_budget():
    from agent_comms.selected_pi_summary_rpc import _summary_response

    request = selected_request()
    frame = summarized_frame(request)
    files = [f"{n}/" + "x" * 3990 for n in range(3800)]
    frame["data"]["result"]["details"]["readFiles"] = files
    raw = (json.dumps(frame) + "\n").encode()
    assert len(raw) > 8 * 1024 * 1024
    result = _summary_response(raw, request, 1200)
    assert result.result.details.read_files == tuple(files)


async def test_retained_native_summary_preserves_source_and_blocks_replay(native_backend):
    """The SDK owns saved entries, the native child owns summary metadata."""
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.owner_compaction_prepare import prepare_native_source
    from agent_comms.owner_compaction_settings import PiCompactionSettings
    from agent_comms.pi_summary_payloads import SelectedModel, SummarySummarizedData
    from agent_comms.selected_source import ManualSource
    from agent_comms.thread_identity import TurnId
    from agent_comms.threads import Thread

    native = native_backend
    settings = PiCompactionSettings(2048, 1)
    configured = json.loads((native.config / "settings.json").read_text())
    configured["compaction"].update(reserveTokens=2048, keepRecentTokens=1)
    (native.config / "settings.json").write_text(json.dumps(configured))
    for text in ("Retained original first question", "Retained original second question"):
        assert (await native.run(text))[-1].ok
    before = native.session.read_bytes()
    prior_inputs = native.saved_inputs()
    child = native.persistent.custody.child
    model = child.attestation.state.model
    assert model is not None and model.provider and model.id and model.context_window
    selected_model = SelectedModel(model.provider, model.id, model.context_window)
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]).resolve(strict=True)
    preparation = (await asyncio.to_thread(
        prepare_native_source,
        package,
        str(native.session),
        settings=settings,
        context_window=model.context_window,
    )).require_ready()

    comms = Comms(native.root)
    comms.registry.declare(
        Thread(
            "summary-owner",
            frozenset(),
            str(native.project),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(native.session),
            model=model.display_name,
        )
    )
    admitted = comms.agents.begin_turn("summary-owner", "native-summary-exchange")
    lease, owner = admitted.turn_lease, admitted.thread
    source = ManualSource(
        owner=owner.require_process(),
        incarnation=owner.incarnation,
        turn=TurnId(lease.turn_id),
        reserved_revision=SessionRevision.observe(str(native.session)).require_available(),
    )
    journal = CompactionJournal(native.root / "compaction-commits.sqlite3")
    slot = SelectedSummarySlot(owner.name, preparation.witness.session_id)
    envelope = SelectedSummarySource(
        source=source,
        selected=selected_model,
        settings=settings,
        retained=RetainedTaskFacts(()),
    )
    events = []

    async def observed(event):
        events.append(event)

    try:
        async with asyncio.timeout(35):
            result = await slot.run_selected_summary(
                native.persistent,
                journal,
                preparation.witness,
                envelope,
                owner=owner,
                expected_package=package,
                tokens_before=preparation.tokens_before,
                custom_instructions="Preserve the two original retained questions",
                idle_timeout_seconds=15,
                on_event=observed,
                reason=ManualCompactionReason,
            )
        assert isinstance(result, SummarySummarizedData)
        assert result.result.summary.strip()
        assert result.result.first_kept_entry_id == preparation.witness.first_kept_entry_id
        assert result.result.tokens_before == preparation.tokens_before
        assert result.result.usage.cost.total >= 0
        assert events
        summary_calls = native.provider.requests[2:]
        assert summary_calls and len(summary_calls) <= 4
        assert "Preserve the two original retained questions" in json.dumps(summary_calls)
        assert native.session.read_bytes() == before
        assert native.saved_inputs() == prior_inputs
        assert journal.summaries.get(result.operation_id).state.declared_name == "reserved"
        assert not native_input_admitted(native.root, str(native.session))
        assert native.persistent.custody.idle().current
        calls = native.provider.posts
        with pytest.raises(CompactionJournalError):
            await slot.run_selected_summary(
                native.persistent,
                journal,
                preparation.witness,
                envelope,
                owner=owner,
                expected_package=package,
                tokens_before=preparation.tokens_before,
                reason=ManualCompactionReason,
            )
        assert native.provider.posts == calls
        assert native.session.read_bytes() == before
    finally:
        comms.agents.finish_turn(lease)
        await native.persistent.close()


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


@pytest.mark.parametrize(
    "mismatch", [None, "operation", "witness", "model", "settings", "missing", "reason"]
)
def test_failed_receipt_requires_exact_attestation(mismatch):
    from agent_comms.pi_summary_payloads import SummaryFailedData
    from agent_comms.selected_pi_summary_rpc import SelectedSummaryFailed, _summary_response

    request = selected_request()
    data = request.to_rpc()
    data.pop("id")
    data.pop("type")
    data.pop("retainedText")
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
