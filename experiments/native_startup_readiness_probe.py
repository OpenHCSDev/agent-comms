"""Offline copied-Pi get_state timing only; disposable synthetic session, no prompt.

Usage: python experiments/native_startup_readiness_probe.py COPIED_PI_PACKAGE [size_mib]
   or: python experiments/native_startup_readiness_probe.py COPIED_PI_PACKAGE --copy SAVED_SESSION
Never launch against the source session. Copies are private, temporary and removed
on exit; no source settings/auth or prompt is sent. Strictly deny network in child.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


async def main() -> None:
    package = Path(sys.argv[1]).resolve()
    copied_source = Path(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--copy" else None
    target_bytes = (
        int(float(sys.argv[2]) * 1024 * 1024)
        if len(sys.argv) > 2 and copied_source is None
        else 112_000_000
    )
    with tempfile.TemporaryDirectory(prefix="native-startup-probe-", dir="/dev/shm") as raw:
        root = Path(raw)
        (root / "project").mkdir(mode=0o700)
        (root / "agent").mkdir(mode=0o700)
        session = root / "session.jsonl"
        header = {
            "type": "session",
            "version": 3,
            "id": "offline-startup-probe",
            "timestamp": "2026-09-25T00:00:00.000Z",
            "cwd": str(root / "project"),
        }
        entries = 0
        size = 0
        previous = None
        if copied_source is not None:
            shutil.copyfile(copied_source, session)
            size = session.stat().st_size
        else:
            with session.open("w", encoding="utf-8") as output:
                line = json.dumps(header, separators=(",", ":")) + "\n"
                output.write(line)
                size += len(line)
                while size < target_bytes:
                    entry = {
                        "type": "message",
                        "id": f"{entries:016x}",
                        "parentId": previous,
                        "timestamp": "2026-09-25T00:00:00.000Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "x" * 1024}],
                            "timestamp": 1790294400000,
                        },
                    }
                    line = json.dumps(entry, separators=(",", ":")) + "\n"
                    output.write(line)
                    size += len(line)
                    previous = entry["id"]
                    entries += 1
        session.chmod(0o600)
        guard = root / "deny-network.cjs"
        guard.write_text(
            "const net=require('node:net');"
            "net.Socket.prototype.connect=()=>{throw Error('PROBE_NETWORK_DENIED')};"
            "globalThis.fetch=()=>{throw Error('PROBE_NETWORK_DENIED')};"
        )
        env = os.environ.copy()
        for key in tuple(env):
            if (
                key.startswith("PI_")
                or key.startswith("AGENT_COMMS_")
                or key.endswith("_API_KEY")
                or key.endswith("_TOKEN")
                or key.endswith("_SECRET")
            ):
                env.pop(key)
        env.update(
            PI_OFFLINE="1",
            PI_CODING_AGENT_DIR=str(root / "agent"),
            NODE_OPTIONS=f"--require={guard}",
        )
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "node",
            str(package / "dist" / "cli.js"),
            "--mode",
            "rpc",
            "--no-approve",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--session-dir",
            str(root),
            "--session",
            str(session),
            "--provider",
            "openrouter",
            "--model",
            "z-ai/glm-5.3-flash",
            cwd=root / "project",
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        elapsed_spawn = time.monotonic() - started
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(b'{"type":"get_state","id":"offline-probe"}\n')
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=75.0)
            if not line:
                assert proc.stderr is not None
                error = (await proc.stderr.read()).decode(errors="replace")
                raise RuntimeError(f"Copied Pi exited before get_state: {error[-3000:]}")
            response = json.loads(line)
            elapsed = time.monotonic() - started
            data = response.get("data") or {}
            assert response.get("type") == "response"
            assert response.get("command") == "get_state"
            assert response.get("id") == "offline-probe"
            assert response.get("success") is True
            assert data.get("nativeInputProofCapability") == "pi-native-input-v1-live-only"
            assert data.get("sessionFile") == str(session)
            print(
                json.dumps(
                    {
                        "size_bytes": size,
                        "entries": entries,
                        "spawn_ms": round(elapsed_spawn * 1000),
                        "ready_ms": round(elapsed * 1000),
                        "capability": data["nativeInputProofCapability"],
                        "session_identity_matched": True,
                        "no_prompt": True,
                        "network_denied": True,
                        "copied_saved_session": copied_source is not None,
                    },
                    sort_keys=True,
                )
            )
        finally:
            if proc.returncode is None:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.communicate(), timeout=4)
            except TimeoutError:
                proc.kill()
                await proc.communicate()


if __name__ == "__main__":
    asyncio.run(main())
