"""No-provider probes of the pinned Pi build's tracked-input compaction gate."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_comms.backend import stream_agent_events


@pytest.mark.parametrize("succeeds", [True, False])
def test_tracked_input_compacts_before_model_send_and_stops_on_failure(succeeds: bool) -> None:
    selected = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not selected:
        pytest.skip("Set AC_NATIVE_COPIED_PACKAGE to the prepared pinned Pi package")
    package = Path(selected)
    assert (package / "dist/core/agent-session.js").is_file()
    script = r"""
import { pathToFileURL } from 'node:url';
const root = process.argv[1];
const { AgentSession } = await import(pathToFileURL(root + '/dist/core/agent-session.js').href);
const long = { role: 'user', content: [{ type: 'text', text: 'x'.repeat(600000) }], timestamp: 1 };
const short = { role: 'user', content: [{ type: 'text', text: 'summary' }], timestamp: 2 };
let calls = 0;
const owner = {
  _nativeRunHadTrackedInput: true,
  model: { contextWindow: 128000 },
  settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 16384, keepRecentTokens: 20000 }) },
  agent: { state: { messages: [long] } },
  _runAutoCompaction: async () => {
    calls++;
    if (process.argv[2] === 'success') owner.agent.state.messages = [short];
    return false;
  },
};
let failed = false;
let result;
try {
  result = await AgentSession.prototype._compactBeforeNextAssistantResponse.call(owner, { messages: [long] });
} catch { failed = true; }
console.log(JSON.stringify({ calls, failed, short: result?.messages?.[0] === short }));
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(package),
            "success" if succeeds else "fail",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    actual = json.loads(result.stdout)
    assert actual == {"calls": 1, "failed": not succeeds, "short": succeeds}


def test_auto_compaction_summary_never_retries_an_uncertain_call() -> None:
    selected = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not selected:
        pytest.skip("Set AC_NATIVE_COPIED_PACKAGE to the prepared pinned Pi package")
    package = Path(selected)
    script = r"""
import { pathToFileURL } from 'node:url';
const { AgentSession } = await import(pathToFileURL(process.argv[1] + '/dist/core/agent-session.js').href);
let calls = 0;
let maxRetries;
const owner = {
  agent: { streamFunction: async (_model, _context, options) => {
    calls++;
    maxRetries = options.maxRetries;
    throw new Error('synthetic provider failure');
  } },
  settingsManager: { getRetrySettings: () => ({ enabled: true, maxRetries: 3, baseDelayMs: 1 }) },
  thinkingLevel: 'off',
  _summarizationRetryCallbacks: () => ({}),
};
const preparation = {
  firstKeptEntryId: 'kept',
  messagesToSummarize: [{ role: 'user', content: [{ type: 'text', text: 'history' }], timestamp: 1 }],
  turnPrefixMessages: [], isSplitTurn: false, tokensBefore: 100,
  previousSummary: undefined, fileOps: { read: new Set(), edited: new Set() },
  settings: { reserveTokens: 16384 },
};
let failed = false;
try {
  await AgentSession.prototype._runDefaultCompaction.call(owner, preparation,
    { provider: 'openrouter', id: 'fake', contextWindow: 128000, maxTokens: 4096 },
    'local-fixture', {}, undefined, new AbortController().signal, {}, 'threshold');
} catch { failed = true; }
console.log(JSON.stringify({ calls, maxRetries, failed }));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(package)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"calls": 1, "maxRetries": 0, "failed": True}


@pytest.mark.parametrize("status", [200, 503])
async def test_tracked_prompt_compacts_locally_before_model_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    selected = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not selected:
        pytest.skip("Set AC_NATIVE_COPIED_PACKAGE to the prepared pinned Pi package")
    from test_manual_compaction import LoopbackProvider, saved_session

    provider = LoopbackProvider(status=status)
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    try:
        provider.port = server.sockets[0].getsockname()[1]
        project = tmp_path / "project"
        project.mkdir()
        session = tmp_path / "saved.jsonl"
        rows = [json.loads(row) for row in saved_session(session).splitlines()]
        # A prior provider failure has no usable usage; a tracked prompt must
        # estimate the retained history before its first model request.
        rows[1]["message"]["content"] = "OLD " * 170_000
        for row in rows:
            message = row.get("message") or {}
            if message.get("role") == "assistant":
                message.pop("usage", None)
        rows[-1]["message"]["stopReason"] = "error"
        rows[-1]["message"]["errorMessage"] = "Previous context request was rejected"
        session.write_text("".join(json.dumps(row) + "\n" for row in rows))
        session.chmod(0o600)
        profile = tmp_path / "profile"
        profile.mkdir(mode=0o700)
        (profile / "settings.json").write_text(
            json.dumps(
                {
                    "retry": {"enabled": True, "maxRetries": 3, "provider": {"maxRetries": 3}},
                    "compaction": {
                        "enabled": True,
                        "reserveTokens": 16384,
                        "keepRecentTokens": 20000,
                    },
                }
            )
        )
        (profile / "auth.json").write_text(
            '{"openrouter":{"type":"api_key","key":"local-fixture"}}'
        )
        (profile / "models.json").write_text(
            json.dumps(
                {"providers": {"openrouter": {"baseUrl": f"http://127.0.0.1:{provider.port}/v1"}}}
            )
        )
        preload = tmp_path / "offline.cjs"
        preload.write_text(
            "const original=globalThis.fetch;"
            "globalThis.fetch=(url,...rest)=>{"
            "const link=url instanceof Request?url.url:String(url);"
            "if(!link.startsWith('http://127.0.0.1:'))"
            "throw Error('BLOCKED_NONLOCAL_NETWORK');"
            "return original(url,...rest);};"
        )
        launcher = tmp_path / "pi-local"
        launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import os,shutil,sys\n"
            'os.execv(shutil.which("node"),'
            '["node",os.environ["TEST_PI_PACKAGE"]+"/dist/cli.js",*sys.argv[1:]])\n'
        )
        launcher.chmod(0o700)
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(profile))
        monkeypatch.setenv("OPENROUTER_API_KEY", "local-fixture")
        monkeypatch.setenv("NODE_OPTIONS", f"--require={preload}")
        monkeypatch.setenv("TEST_PI_PACKAGE", selected)

        events = []
        async with asyncio.timeout(30):
            async for event in stream_agent_events(
                str(launcher),
                ["--print", "--provider", "openrouter", "--model", "openai/gpt-4o-mini"],
                "new tracked input",
                str(project),
                session_file=str(session),
                require_input_id=True,
            ):
                events.append(event)

        assert any(event["type"] == "compaction_start" for event in events), events[-5:]
        assert events[-1]["type"] == "done"
        if status == 200:
            assert provider.posts >= 2, events[-5:]
            assert events[-1]["ok"] is True, events[-5:]
        else:
            assert provider.posts == 1, events[-5:]
            assert events[-1]["ok"] is False, events[-5:]
    finally:
        server.close()
        await server.wait_closed()
