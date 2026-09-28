"""Provider-free fake selected-child dry-run RPC; no Pi patch or paid path."""

import asyncio
import json

import pytest

from agent_comms.backend import PersistentPiSession, _session_revision
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_prepare import NativeWitness
from agent_comms.selected_pi_route import SelectedPiProbeUnknownError, probe_idle_selected_pi


class FakeStdin:
    def __init__(self):
        self.request = None
        self.closed = False

    def write(self, raw):
        self.request = json.loads(raw)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self):
        self.stdin = FakeStdin()
        self.returncode = None
        self.pid = 1


class FakeReader:
    def __init__(self, proc, response):
        self.proc = proc
        self.response = response
        self.calls = 0

    async def readline(self, *, max_bytes=None):
        self.calls += 1
        if self.response is None:
            await asyncio.Future()
        value = self.response(self.proc.stdin.request)
        return (json.dumps(value) + "\n").encode()


def request_case(tmp_path, response):
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","version":3,"id":"session-id"}\n')
    persistent = PersistentPiSession()
    proc = FakeProcess()
    reader = FakeReader(proc, response)
    persistent.proc = proc  # exact pre-existing child; adapter must never spawn
    persistent.reader = reader
    persistent.session_file = str(session)
    persistent.session_id = "session-id"
    persistent.revision = _session_revision(str(session))
    persistent.launch_key = ("pi-native",)
    witness = FieldCodec.decode(
        NativeWitness,
        {
            "sessionId": "session-id",
            "sessionFile": str(session),
            "leafId": "leaf",
            "firstKeptEntryId": "kept",
            "revision": "1:2:3:4:5",
        },
    )
    selected = {"provider": "fixture", "modelId": "model", "contextWindow": 1000}
    settings = {"reserveTokens": 50, "keepRecentTokens": 20}
    return persistent, proc, reader, witness, selected, settings, session


def ready(request):
    return {
        "id": request["id"],
        "type": "response",
        "command": request["type"],
        "success": True,
        "data": {
            "version": 1,
            "status": "ready",
            "routeStatus": "UNVERIFIED_NO_AUTH_RESOLUTION",
            "witness": dict(request["witness"]),
            "selected": dict(request["selected"]),
            "settings": dict(request["settings"]),
        },
    }


async def probe(case, **kwargs):
    persistent, _, _, witness, selected, settings, _ = case
    return await probe_idle_selected_pi(
        persistent, witness, selected, settings, expected_launcher="pi-native", **kwargs
    )


async def test_ready_is_non_authorizing_selected_existing_child_only(tmp_path):
    case = request_case(tmp_path, ready)
    before = case[-1].read_bytes()
    outcome = await probe(case)
    assert outcome.status == "ready"
    assert outcome.route_status == "UNVERIFIED_NO_AUTH_RESOLUTION"
    with pytest.raises(TypeError, match="never authorizes"):
        bool(outcome)
    request = case[1].stdin.request
    assert set(request) == {"id", "type", "version", "dryRun", "witness", "selected", "settings"}
    assert len(request["id"]) == 32 and request["dryRun"] is True
    assert request["type"] == "agent_comms_prepare_compaction"
    assert case[2].calls == 1
    assert case[-1].read_bytes() == before
    assert case[0].reopen_required is None


async def test_declined_is_bounded_not_a_paid_summary(tmp_path):
    def decline(request):
        return {
            "id": request["id"],
            "type": "response",
            "command": request["type"],
            "success": True,
            "data": {"version": 1, "status": "declined", "reason": "split_turn"},
        }

    case = request_case(tmp_path, decline)
    outcome = await probe(case)
    assert outcome.status == "declined" and outcome.reason == "split_turn"
    assert case[0].reopen_required is None


@pytest.mark.parametrize("alter", ["id", "route", "context_type", "unexpected"])
async def test_malformed_or_overclaimed_ready_poison_old_child(tmp_path, alter):
    def altered(request):
        response = ready(request)
        if alter == "id":
            response["id"] = "0" * 32
        elif alter == "route":
            response["data"]["routeStatus"] = "VERIFIED"
        elif alter == "context_type":
            response["data"]["selected"]["contextWindow"] = True
            request["selected"]["contextWindow"] = 1  # Equal to bool under loose comparison.
        else:
            response["data"]["summary"] = "unauthorized"
        return response

    case = request_case(tmp_path, altered)
    closed = []

    async def close():
        closed.append(True)
        case[0].proc = None

    case[0].close = close
    with pytest.raises(SelectedPiProbeUnknownError):
        await probe(case)
    assert case[0].reopen_required == str(case[-1])
    assert closed == [True]
    assert case[2].calls == 1


async def test_timeout_is_unknown_without_replay_and_requires_reopen(tmp_path):
    case = request_case(tmp_path, None)
    closed = []

    async def close():
        closed.append(True)
        case[0].proc = None

    case[0].close = close
    with pytest.raises(SelectedPiProbeUnknownError):
        await probe(case, timeout=0.01)
    assert closed == [True] and case[2].calls == 1
    assert case[0].reopen_required == str(case[-1])


async def test_cancelled_sent_probe_poisoned_without_retry(tmp_path):
    case = request_case(tmp_path, None)
    closed = []

    async def close():
        closed.append(True)
        case[0].proc = None

    case[0].close = close
    task = asyncio.create_task(probe(case))
    for _ in range(20):
        if case[2].calls:
            break
        await asyncio.sleep(0)
    assert case[2].calls == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]
    assert case[0].reopen_required == str(case[-1])
    assert case[2].calls == 1


async def test_stale_child_refuses_before_rpc(tmp_path):
    case = request_case(tmp_path, ready)
    case[0].revision = None
    with pytest.raises(SelectedPiProbeUnknownError, match="stale"):
        await probe(case)
    assert case[1].stdin.request is None
    assert case[2].calls == 0


def selected_settings(request):
    return {
        "id": request["id"],
        "type": "response",
        "command": request["type"],
        "success": True,
        "data": {
            "version": 1,
            **{
                key: request[key]
                for key in ("sessionId", "sessionFile", "selected", "contextTokens")
            },
            "decision": {
                "enabled": True,
                "reserveTokens": 100,
                "keepRecentTokens": 20,
                "trigger": True,
            },
        },
    }


async def settings_probe(case):
    from agent_comms.selected_pi_route import read_selected_compaction_decision

    persistent, _, _, witness, selected, _, _ = case
    return await read_selected_compaction_decision(
        persistent,
        session_file=witness.session_file,
        expected_launcher="pi-native",
        provider=selected["provider"],
        model_id=selected["modelId"],
        context_tokens=950,
        context_window=selected["contextWindow"],
    )


async def test_selected_settings_reads_once_without_starting_or_writing(tmp_path):
    case = request_case(tmp_path, selected_settings)
    result = await settings_probe(case)
    assert result.enabled and result.trigger and result.keep_recent_tokens == 20
    assert case[2].calls == 1
    assert case[0].reopen_required is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(sessionId="other"),
        lambda data: data.update(contextTokens=True),
        lambda data: data["selected"].update(contextWindow=True),
        lambda data: data["selected"].update(provider="other"),
        lambda data: data["decision"].update(reserveTokens=True),
        lambda data: data["decision"].update(trigger="yes"),
        lambda data: data["decision"].update(keepRecentTokens=0),
    ],
)
async def test_selected_settings_uncertain_response_retires_child(tmp_path, mutation):
    def response(request):
        reply = selected_settings(request)
        mutation(reply["data"])
        return reply

    case = request_case(tmp_path, response)
    closed = []

    async def close():
        closed.append(True)
        case[0].proc = None

    case[0].close = close
    with pytest.raises(SelectedPiProbeUnknownError):
        await settings_probe(case)
    assert closed == [True]
    assert case[2].calls == 1
    assert case[0].reopen_required == str(case[-1])
    assert case[0].proc is None
