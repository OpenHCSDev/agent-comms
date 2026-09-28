"""Actual local child transport and transaction lifetime; no provider connection."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from agent_comms import backend, manual_compaction
from agent_comms.manual_compaction import ManualCompaction

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("node") is None, reason="POSIX and Node local fixture"
)


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    package = tmp_path / "package"
    core = package / "dist/core"
    core.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module"}')
    (core / "session-manager.js").write_text("""
import {readFileSync} from 'node:fs';
export const SessionManager = {open(file) {
    const header = JSON.parse(readFileSync(file, 'utf8').split('\\n')[0]);
    return {getSessionId: () => header.id, getSessionFile: () => file};
}};
""")
    monkeypatch.setattr(manual_compaction, "_pinned_package", lambda: package)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "empty-credentials"))
    session = tmp_path / "saved.jsonl"
    session.write_text(json.dumps({"type": "session", "version": 3, "id": "original"}) + "\n")
    executable = tmp_path / "pi-local"
    executable.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, sys, time
file = pathlib.Path(sys.argv[sys.argv.index('--session') + 1])
requests = file.with_suffix('.requests')
mode = os.environ.get('MANUAL_FIXTURE_MODE', 'success')
for line in sys.stdin:
    request = json.loads(line)
    with requests.open('a') as log: log.write(line)
    if request['type'] == 'get_state':
        data = {'sessionFile': str(file), 'sessionId': 'original',
                'model': {'provider': 'openrouter', 'id': 'local'}}
    else:
        if mode == 'hang':
            while True: time.sleep(.05)
        if mode != 'missing-row':
            with file.open('a') as out:
                out.write(json.dumps({'type':'compaction','id':'committed','summary':'kept'})+'\n')
        data = {'summary': '## Kept\nOriginal facts', 'tokensBefore': 1000}
    print(json.dumps({'type': 'response', 'id': 'foreign', 'command': request['type'],
                      'success': True, 'data': {}}), flush=True)
    print(json.dumps({'type': 'response', 'id': request['id'],
                      'command': 'get_state' if mode == 'wrong-command' else request['type'],
                      'success': True, 'data': data}), flush=True)
"""
    )
    executable.chmod(0o700)
    return ManualCompaction(
        str(executable),
        ["--provider", "openrouter", "--model", "local"],
        str(session),
        str(tmp_path),
        "Keep the original facts",
        timeout_seconds=5,
    )


@pytest.mark.parametrize("instructions", ["Keep the original facts", "   "])
async def test_one_owner_retains_source_and_correlates_each_typed_command(
    transaction, instructions
):
    transaction.instructions = instructions
    original = Path(transaction.session_file).read_bytes()
    result = await transaction.run()
    assert result == {"ok": True, "summary": "## Kept\nOriginal facts", "tokensBefore": 1000}
    assert transaction.proc.returncode == 0
    assert not transaction.profile.exists()
    assert Path(transaction.session_file).read_bytes().startswith(original)
    requests = [
        json.loads(line)
        for line in Path(transaction.session_file).with_suffix(".requests").read_text().splitlines()
    ]
    assert [r["type"] for r in requests] == ["get_state", "compact"]
    assert len({r["id"] for r in requests}) == 2
    if instructions.strip():
        assert requests[-1]["customInstructions"] == instructions
    else:
        assert "customInstructions" not in requests[-1]
    with pytest.raises(RuntimeError, match="never replay"):
        await transaction.run()
    assert not hasattr(backend, "compact_session")
    assert not hasattr(manual_compaction, "compact_session")
    assert not hasattr(manual_compaction, "_compact_session_under_fence")


@pytest.mark.parametrize("mode", ["missing-row", "wrong-command"])
async def test_response_without_exact_committed_result_never_succeeds(
    transaction, monkeypatch, mode
):
    monkeypatch.setenv("MANUAL_FIXTURE_MODE", mode)
    result = await transaction.run()
    assert not result["ok"]
    assert transaction.proc.returncode is not None
    assert not transaction.profile.exists()
    requests = Path(transaction.session_file).with_suffix(".requests").read_text().splitlines()
    assert sum(json.loads(line)["type"] == "compact" for line in requests) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_unknown_attempt_reaps_child_and_does_not_replay(transaction, monkeypatch, cancel):
    monkeypatch.setenv("MANUAL_FIXTURE_MODE", "hang")
    if not cancel:
        transaction.timeout = 0.4
    task = asyncio.create_task(transaction.run())
    requests = Path(transaction.session_file).with_suffix(".requests")
    async with asyncio.timeout(3):
        while not requests.exists() or len(requests.read_text().splitlines()) != 2:
            await asyncio.sleep(0.01)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert not result["ok"] and "timed out" in result["error"]
    assert transaction.proc.returncode is not None
    assert not transaction.profile.exists()
    assert len(requests.read_text().splitlines()) == 2
    with pytest.raises(RuntimeError, match="never replay"):
        await transaction.run()
