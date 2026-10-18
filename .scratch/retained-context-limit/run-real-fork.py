from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path

from agent_comms import agent_events as ae
from agent_comms import backend
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.pi_vocabulary import ThresholdCompactionReason


async def main() -> None:
    session_file = os.environ["FORK_SESSION"]
    project = os.environ["FORK_PROJECT"]
    native_bin = os.environ["AC_NATIVE_STACK_BIN"]
    events: list[ae.AgentEvent] = []
    persistent = PersistentPiSession()
    try:
        async with asyncio.timeout(900):
            async for event in backend.stream_agent_events(
                native_bin,
                (
                    "--offline",
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "--no-context-files",
                    "--no-tools",
                    "--provider",
                    "openai-codex",
                    "--model",
                    "gpt-5.6-sol",
                    "--thinking",
                    "high",
                ),
                "Reply with exactly: FORK_LIMIT_OK",
                project,
                env_extra={
                    "PI_CODING_AGENT_DIR": os.environ["FORK_AGENT_DIR"],
                    "AGENT_COMMS_NATIVE_CONFIG_DIR": "/home/ts/.pi/agent",
                    "PI_TASK": "Verify Core's 300,000-token retained-context limit on an isolated real-session fork.",
                },
                session_file=session_file,
                require_input_id=True,
                native_start=lambda *_: True,
                persistent_session=persistent,
            ):
                events.append(event)
    finally:
        await persistent.close()

    ends = [event for event in events if isinstance(event, ae.CompactionEnd)]
    done = events[-1]
    if not isinstance(done, ae.Done) or not done.ok:
        raise AssertionError(f"fork turn failed: {done!r}")
    if len(ends) != 1 or ends[0].reason is not ThresholdCompactionReason or ends[0].aborted:
        raise AssertionError(f"expected one threshold compaction, got {ends!r}")

    rows = [json.loads(line) for line in Path(session_file).read_text().splitlines()]
    compactions = [row for row in rows if row.get("type") == "compaction"]
    final_assistant = next(
        row["message"]
        for row in reversed(rows)
        if row.get("type") == "message" and row.get("message", {}).get("role") == "assistant"
    )
    summary = compactions[-1]["summary"]
    expected = (
        "Now: validating the hard retained-context limit on this isolated real-session fork",
        "Uncommitted: PR #824 source is pushed; acceptance evidence remains local",
        "Next: confirm one threshold compaction and a next-turn context below 300,000 tokens",
    )
    if not all(line in summary for line in expected):
        raise AssertionError("work-in-progress lines did not survive compaction")
    total = final_assistant["usage"]["totalTokens"]
    if total >= 300000:
        raise AssertionError(f"post-compaction context remained above the limit: {total}")
    text = "\n".join(
        block.get("text", "")
        for block in final_assistant.get("content", [])
        if block.get("type") == "text"
    )
    if "FORK_LIMIT_OK" not in text:
        raise AssertionError(f"unexpected fork reply: {text!r}")
    print(json.dumps({
        "event_types": [type(event).__name__ for event in events],
        "compaction_events": len(ends),
        "compaction_reason": ends[0].reason.__name__,
        "compaction_entries_before_fixture": 91,
        "compaction_entries_after": len(compactions),
        "summary_starts": summary[:700],
        "post_compaction_total_tokens": total,
        "final_text": text,
        "done": done.ok,
    }, indent=2))


asyncio.run(main())
