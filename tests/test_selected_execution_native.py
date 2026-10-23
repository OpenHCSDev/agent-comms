"""SelectedExecution with pinned native Pi, local deterministic coding provider."""
from agent_comms.lifecycle_capability import Complete
from bus_history import read_history
from agent_comms.pi_vocabulary import LowThinkingLevel
from binding_reads import binding_rows

import asyncio
import json
import os
import shlex
import threading
import traceback
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination
from agent_comms.agent_activity import AgentActivity
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_input_record import TriageNativeExecution, FullNativeExecution
from agent_comms.native_pi import CommittedInput, read_tracked_input_commitment
from agent_comms.native_package import VerifiedNativePackage

from agent_comms.native_tools import NativeToolDeclaration
from agent_comms.cursor_owner import CursorOwner
from agent_comms.selected_tool_broker import SelectedToolIntent
from test_coordinated_runtime import _root
from test_coordinated_runtime import tmp_path as private_root_fixture
from agent_comms.thread_model import DeclaredModel
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase

tmp_path = private_root_fixture


@pytest.mark.parametrize(
    "selected_write, triage",
    [(False, False), (True, False), (False, True)],
)
async def test_native_full_four_tools_publish_and_release(tmp_path, monkeypatch, selected_write, triage, *, cursor_delivery, source_reads):
    package = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not package:
        pytest.skip("Prepared native package required; never build or call a paid provider")
    root, root_id, comms, initial, people = _root(tmp_path, direct=not triage, claims=True)
    owner = comms.registry.require("beta")
    comms.registry.register(replace(owner, model=DeclaredModel("selected-offline/fixture"), thinking_level=LowThinkingLevel))
    (tmp_path / "input.txt").write_text("state=BEFORE\n")
    requests = []
    failures = []
    phases = []
    native_phases = []
    model_wait_after_tools = threading.Event()
    observe_native_phase = AgentActivity.observe_native_phase

    def record_native_phase(agents, lease, phase):
        states = observe_native_phase(agents, lease, phase)
        if agents.registry.store.path.parent == root and states:
            native_phases.append(type(lease.record.read().require_active().phase))
            if native_phases[-2:] == [ToolRunningPhase, ModelWaitPhase]:
                model_wait_after_tools.set()
        return states

    monkeypatch.setattr(AgentActivity, "observe_native_phase", record_native_phase)
    calls = [
        ("read", {"path": "input.txt"}),
        ("edit", {"path": "input.txt", "edits": [{"oldText": "BEFORE", "newText": "AFTER"}]}),
        ("write", {"path": "nested/result.txt", "content": "state=AFTER\n"}),
        (
            "bash",
            {
                "command": (
                    "python -c \"from pathlib import Path; assert Path('input.txt').read_text() == "
                    "Path('nested/result.txt').read_text() == 'state=AFTER\\n'\""
                )
            },
        ),
    ]

    if selected_write:
        calls = [("selected_claimed_write", {"resource": "input.txt", "contents": "state=AFTER\n"})]

    if entrypoint := os.environ.get('AC_HEADLESS_ENTRYPOINT'):
        assert not selected_write
        inspection = (
            'from pathlib import Path; from agent_comms.comms import wire; from agent_comms.publisher import Publisher; '
            'from agent_comms.turn_phase import ToolRunningPhase; '
            f'c=wire(Path({str(root)!r}), Publisher); '
            'assert isinstance(c.registry.require("beta").turn_record.read().require_active().phase, ToolRunningPhase); '
            'print("CANONICAL_TOOL_RUNNING")'
        )
        python = shlex.quote(str(Path(entrypoint).with_name('python')))
        entrypoint = shlex.quote(entrypoint)
        calls[-1][1]['command'] += (
            f'; {python} -c {shlex.quote(inspection)}; '
            f'{entrypoint} tools --help; {entrypoint} --help; '
            f'{entrypoint} tools; test $? -eq 2'
        )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                phase = comms.registry.require('beta').turn_record.read().require_active().phase
                phases.append(phase.declared_name)
                assert request["model"] == "fixture"
                assert self.headers["Authorization"] == "Bearer offline-only-fixture"
                assert len(requests) <= 2 + triage
                if triage and len(requests) == 1:
                    assert "Output ONLY a JSON object with one key" in json.dumps(request)
                    assert not request.get("tools")
                    delta = {"role": "assistant", "content": '{"decision":"FULL"}'}
                    reason = "stop"
                elif len(requests) == 1 + triage:
                    if not triage:
                        context = json.dumps(request["messages"])
                        assert "Selected source decisions through " in context
                        assert "Optional non-authoritative awareness" in context
                        assert initial.message.message_id in context
                    declared = ({name for name, _ in calls} if selected_write else {
                        tool.declared_name
                        for tool in NativeToolDeclaration.members_with(NativeToolDeclaration)
                    })
                    assert {t["function"]["name"] for t in request["tools"]} == declared
                    delta = {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": i,
                                "id": f"coding_{i}",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                            for i, (name, arguments) in enumerate(calls)
                        ],
                    }
                    reason = "tool_calls"
                else:
                    tools = [m for m in request["messages"] if m["role"] == "tool"]
                    assert len(tools) == len(calls)
                    assert (tmp_path / "input.txt").read_text() == "state=AFTER\n"
                    if not selected_write:
                        assert (tmp_path / "nested/result.txt").read_text() == "state=AFTER\n"
                    assert not any("Error:" in str(t["content"]) for t in tools)
                    if os.environ.get('AC_HEADLESS_ENTRYPOINT'):
                        assert 'CANONICAL_TOOL_RUNNING' in str(tools[-1]['content'])
                    # Pi's next HTTP request can outrun Core's observation of
                    # the tool terminal. Hold this response until the real
                    # phase owner commits the transition back to model wait.
                    assert model_wait_after_tools.wait(5), native_phases
                    assert 'participant' not in comms.registry.snapshot().threads
                    delta = {"role": "assistant", "content": "CODING_TOOLS_OK"}
                    reason = "stop"
                chunk = {
                    "id": "selected-offline",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                final = {
                    **chunk,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                }
                body = (
                    "".join("data: " + json.dumps(v) + "\n\n" for v in (chunk, final))
                    + "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                failures.append(traceback.format_exc())
                self.send_error(400, "Offline fixture assertion failed")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "canonical-offline"
    config.mkdir(mode=0o700)
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "selected-offline": {
                        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        "api": "openai-completions",
                        "models": [
                            {
                                "id": "fixture",
                                "name": "Offline fixture",
                                "contextWindow": 32768,
                                "maxTokens": 2048,
                            }
                        ],
                    }
                }
            }
        )
    )
    (config / "auth.json").write_text(
        json.dumps({"selected-offline": {"type": "api_key", "key": "offline-only-fixture"}})
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    execution = SelectedExecution(
        root=root,
        wire_root_id=root_id,
        owner_name="beta",
        native_package=VerifiedNativePackage.acquire(Path(package)),
        selected_tool_intent=SelectedToolIntent() if selected_write else None,
        cursor_delivery=cursor_delivery,
    )
    try:
        outcome = await asyncio.wait_for(execution.run(), 40)
        assert len(requests) == 2 + triage
        assert outcome.publications
        responses = [
            m for m in read_history(comms) if m.reference in tuple(acknowledgement.reference for acknowledgement in outcome.publications)
        ]
        assert len(responses) == 1 and responses[0].body == "CODING_TOOLS_OK"
        assert not comms.registry.require('beta').executing
        assert phases and 'preparing' not in phases
        if not selected_write:
            assert not comms.bus.bus.claim_projection()
        with Coordination(str(root / "coordination.sqlite3")) as store:
            assignment = store.assignments.get(outcome.assignment_ids[0])
            snapshot = store.get(assignment.lifecycle.require_completion().execution_id)
            snapshot.execution.lifecycle.require(Complete, ValueError("execution must complete"))
            assert (
                snapshot.attempt.lifecycle.backend_done and snapshot.attempt.lifecycle.process_dead
            )
            assert assignment.wire_seq == initial.message.seq
            state = await CursorOwner.read_async(
                comms.bus, wire_root_id=root_id,
                owner_name="beta", source_reads=source_reads,
            )
            assert state.injected_seq == initial.message.seq
            assert state.covered_seq >= state.injected_seq
            assert state.proves_input(outcome.input_id)
            proofs = read_historical_native_inputs(
                store,
                wire_root_id=root_id,
                recipient_lookup=RecordedThreadIncarnation(
                    "fixture", owner.created_at
                ).lookup,
                source_seq=initial.message.seq,
                source_reads=source_reads,
            )
            assert len(proofs) == 1 + triage
            assert {type(proof.execution) for proof in proofs} == ({TriageNativeExecution, FullNativeExecution} if triage else {FullNativeExecution})
            assert (
                next(proof for proof in proofs if isinstance(proof.execution, FullNativeExecution)).input_id
                == outcome.input_id
            )
            for proof in proofs:
                assert proof.expected_prompt_equality_established
                binding = binding_rows(store, (proof.input_id,)).get(proof.input_id)
                assert binding is not None
                assert (
                    read_tracked_input_commitment(proof.context.session_file, proof.input_id)
                    == CommittedInput(binding.expected_prompt_digest)
                )
            if triage:
                assert len({proof.context.session_file for proof in proofs}) == 1
                assert len({proof.input_id for proof in proofs}) == 2
        with pytest.raises(IdentityConflict):
            await execution.run()
        assert len(requests) == 2 + triage
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not failures, "\n\n".join(failures)
