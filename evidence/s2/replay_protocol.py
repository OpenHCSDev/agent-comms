"""Compare pre-S2 and current runners against local recorded protocol scenarios.

This is a deterministic protocol replay, not a claim that synthetic transcripts
are production provider recordings. Actual pinned CLI proofs are run separately.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from agent_comms import backend

ARTIFACT = ROOT / ".artifacts/s2/replay" / str(time.time_ns())
ARTIFACT.mkdir(parents=True, exist_ok=True)
source = subprocess.check_output(
    ["git", "show", "f60abbb:src/agent_comms/backend.py"], cwd=ROOT, text=True
)
module_file = ARTIFACT / "baseline_backend.py"
module_file.write_text(source)
spec = importlib.util.spec_from_file_location("agent_comms.s2_baseline", module_file)
old = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = old
spec.loader.exec_module(old)

assistant = {"role": "assistant", "content": [{"type": "text", "text": "ok"}], "stopReason": "stop", "usage": {"totalTokens": 40}}
normal = [
    {"type": "agent_start"},
    {"type": "message_start", "message": {"role": "assistant"}},
    {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "ok"}},
    {"type": "message_end", "message": assistant},
    {"type": "agent_end"}, {"type": "agent_settled"},
]
SCENARIOS = {
    "normal": normal,
    "tools": [{"type": "tool_execution_start", "toolName": "bash", "toolCallId": "1", "args": {"command": "true"}}, {"type": "tool_execution_update", "toolName": "bash", "toolCallId": "1", "partialResult": {"content": [{"text": "progress"}]}}, {"type": "tool_execution_end", "toolName": "bash", "toolCallId": "1", "result": {"content": [{"text": "done"}]}}, *normal],
    "retry": [{"type": "auto_retry_start", "attempt": 1, "maxAttempts": 2}, {"type": "auto_retry_end", "success": True}, *normal],
    "summary_retry": [{"type": "summarization_retry_scheduled", "attempt": 1, "maxAttempts": 2}, {"type": "summarization_retry_attempt_start", "attempt": 2, "maxAttempts": 2}, {"type": "summarization_retry_finished", "success": True}, *normal],
    "compaction": [{"type": "compaction_start", "reason": "threshold"}, {"type": "compaction_progress", "chunkIndex": 1, "usage": {"totalTokens": 12}}, {"type": "compaction_end", "reason": "threshold", "aborted": False, "result": {"summary": "kept", "usage": {"totalTokens": 12}}}, *normal],
    "compaction_abort": [{"type": "compaction_start"}, {"type": "compaction_end", "aborted": True}, *normal],
    "interrupt": [{"type": "steering_interrupt_started"}, {"type": "steering_interrupt_completed"}, *normal],
    "unknown_malformed": ["not-json", {"type": "future_event", "unowned": True}, *normal],
    "ui": [{"type": "extension_ui_request", "id": "dialog", "method": "confirm", "title": "Local replay"}, *normal],
    "provider_error": [{"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "fixture refusal"}}, {"type": "agent_settled"}],
}


def normalize(value):
    if dataclasses.is_dataclass(value):
        return {"event": type(value).__name__, **normalize(dataclasses.asdict(value))}
    if isinstance(value, dict):
        return {k: (0 if k in {"elapsed_ms", "wait_ms", "spawn_ms"} else normalize(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    return value


async def replay(module, name, records):
    directory = ARTIFACT / name / module.__name__
    directory.mkdir(parents=True, exist_ok=True)
    outbound = directory / "commands.jsonl"
    child = directory / "pi"
    child.write_text(f'''#!{sys.executable}
import json,sys
records={records!r}
def emit(row):
    print(row if isinstance(row,str) else json.dumps(row),flush=True)
for line in sys.stdin:
    row=json.loads(line)
    with open({str(outbound)!r},'a') as log: log.write(json.dumps(row)+'\\n')
    kind=row['type']
    if kind=='get_state': emit({{'type':'response','command':kind,'id':row.get('id'),'success':True,'data':{{'sessionId':'replay','model':{{'id':'configured','provider':'fixture'}}}}}})
    elif kind=='get_session_stats': emit({{'type':'response','command':kind,'success':True,'data':{{'contextUsage':{{'tokens':40}}}}}})
    elif kind=='prompt':
        emit({{'type':'response','command':kind,'id':row['id'],'success':True}})
        emit({{'type':'message_start','message':{{'role':'user','content':row['message'],'inputId':row['inputId']}}}})
        for event in records: emit(event)
''')
    child.chmod(0o700)
    output = [normalize(event) async for event in module.stream_agent_events(str(child), [], "test", str(directory), require_input_id=False)]
    commands = [json.loads(line) for line in outbound.read_text().splitlines()]
    # Native IDs differ between independent attempts; identity relationships are
    # covered by the input-binding suite. All other command fields compare exactly.
    for row in commands:
        if "inputId" in row: row["inputId"] = "NATIVE_INPUT"
        if row.get("id", "").startswith("agent-comms-preflight-"): row["id"] = "PREFLIGHT"
    return {"events": output, "commands": commands}


async def main():
    for name, records in SCENARIOS.items():
        before = await replay(old, name, records)
        after = await replay(backend, name, records)
        (ARTIFACT / f"{name}.json").write_text(json.dumps({"before": before, "after": after}, indent=2))
        assert before == after, name
        print(name, "equivalent", flush=True)


asyncio.run(main())
