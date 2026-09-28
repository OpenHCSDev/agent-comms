"""Offline fresh/copied CLI acceptance for a staged immutable native bundle."""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from agent_comms.child_process import AttachedChild
from agent_comms.native_package import MANIFEST, verify_native_package


async def check(deployment: Path, root: Path) -> dict:
    package = deployment / "node_modules/@earendil-works/pi-coding-agent"
    manifest = deployment / "pi-native.sha256"
    assert manifest.read_bytes() == MANIFEST.read_bytes()
    verify_native_package(package)
    paths = [deployment, *deployment.rglob("*")]
    assert all(not path.lstat().st_mode & 0o222 for path in paths)
    guard = root / "deny-network.mjs"
    guard.write_text(
        "import http from 'node:http';import https from 'node:https';"
        "import net from 'node:net';import {syncBuiltinESMExports} from 'node:module';"
        "const deny=()=>{throw Error('NETWORK_PROHIBITED_IN_DEPLOYMENT_ACCEPTANCE')};"
        "http.request=http.get=https.request=https.get=net.connect=net.createConnection=deny;"
        "globalThis.fetch=deny;syncBuiltinESMExports();"
    )
    node = shutil.which("node")
    assert node
    results = []

    async def launch(name: str, session: Path | None = None, session_id: str | None = None):
        owned = root / name
        owned.mkdir()
        config = owned / "config"
        config.mkdir()
        (config / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "fixture": {
                            "baseUrl": "http://127.0.0.1:1/v1",
                            "api": "openai-completions",
                            "models": [
                                {
                                    "id": "fixture",
                                    "name": "fixture",
                                    "contextWindow": 32000,
                                    "maxTokens": 4096,
                                }
                            ],
                        }
                    }
                }
            )
        )
        (config / "settings.json").write_text(
            json.dumps(
                {
                    "compaction": {"reserveTokens": 1000, "keepRecentTokens": 1000},
                }
            )
        )
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(owned),
            "TMPDIR": str(owned),
            "PI_CODING_AGENT_DIR": str(config),
            "PI_OFFLINE": "1",
            "NODE_DISABLE_COMPILE_CACHE": "1",
            "AGENT_COMMS_SESSION_INDEX_DIR": str(owned / "indexes"),
        }
        command = (
            node,
            "--no-global-search-paths",
            "--import",
            str(guard),
            "--import",
            str(package / "dist/agent-comms-import-fence.mjs"),
            "--import",
            str(package / "dist/agent-comms-project-bootstrap.mjs"),
            str(package / "dist/cli.js"),
            "--mode",
            "rpc",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-tools",
            "--provider",
            "fixture",
            "--model",
            "fixture",
            "--session-dir",
            str(owned / "sessions"),
        )
        if session is not None:
            command += ("--session", str(session))
        child = await AttachedChild.start(command, cwd=owned, env=env)
        stderr = asyncio.create_task(child.stderr.read())
        states = []
        try:
            async with asyncio.timeout(25):
                for request_id, command_name in enumerate(
                    ("get_state", "get_state", "get_messages")
                ):
                    child.stdin.write(
                        (json.dumps({"id": str(request_id), "type": command_name}) + "\n").encode()
                    )
                    await child.stdin.drain()
                    while True:
                        raw = await child.stdout.readline()
                        assert raw, f"{name}: CLI exited before {command_name}"
                        event = json.loads(raw)
                        assert event.get("type") not in ("input_committed", "context_committed")
                        if event.get("type") == "response" and event.get("id") == str(request_id):
                            assert event["success"], event
                            break
                    data = event["data"]
                    if command_name == "get_state":
                        assert (
                            data["nativeInputProofCapability"] == "pi-native-input-v1-live-only"
                        ), data
                        assert data["sessionId"], data
                        selected = Path(data["sessionFile"])
                        assert selected == selected.resolve(), data
                        if session is not None:
                            assert selected == session and data["sessionId"] == session_id, data
                        else:
                            assert selected.is_relative_to(owned / "sessions"), data
                        states.append((data["sessionId"], str(selected)))
                    else:
                        assert len(data["messages"]) == (2 if session else 0), data
                assert states[0] == states[1]
        finally:
            await child.finish()
            errors = (await stderr).decode(errors="replace")
            if errors:
                print(f"{name} stderr: {errors}", file=sys.stderr)
            assert not child.identity.alive()
            assert "NETWORK_PROHIBITED" not in errors, errors
        results.append(
            {
                "case": name,
                "session_id": states[0][0],
                "session_file": states[0][1],
                "identity_stable": True,
                "native_input_capability": "pi-native-input-v1-live-only",
                "message_count": 2 if session else 0,
                "child_reaped": True,
                "stderr": errors,
            }
        )

    await launch("fresh")
    source = root / "synthetic-source.jsonl"
    timestamp = "2026-09-28T17:00:00Z"
    rows = [
        {
            "type": "session",
            "version": 3,
            "id": "deployment-copied-identity",
            "cwd": str(root),
            "timestamp": timestamp,
        },
        {
            "type": "message",
            "id": "user1",
            "parentId": None,
            "timestamp": timestamp,
            "message": {
                "role": "user",
                "content": "synthetic deployment handshake",
                "timestamp": 1,
            },
        },
        {
            "type": "message",
            "id": "assistant1",
            "parentId": "user1",
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "synthetic saved response"}],
                "provider": "fixture",
                "model": "fixture",
                "api": "fixture",
                "stopReason": "stop",
                "timestamp": 2,
            },
        },
    ]
    original = "".join(json.dumps(row) + "\n" for row in rows).encode()
    source.write_bytes(original)
    source.chmod(0o600)
    copied = root / "copied-session.jsonl"
    shutil.copy2(source, copied)
    await launch("copied", copied, "deployment-copied-identity")
    assert source.read_bytes() == original
    saved = copied.read_bytes()
    assert saved.startswith(original)
    appended = [json.loads(line) for line in saved[len(original):].splitlines()]
    assert all(
        row["type"] in {"model_change", "thinking_level_change"} for row in appended
    ), appended
    return {
        "deployment": str(deployment),
        "canonical_package": str(package.resolve()),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "manifest_matches_current_receiver": True,
        "package_verification": "passed",
        "allocated_bytes": sum(path.lstat().st_blocks * 512 for path in paths),
        "write_bits_removed": True,
        "import_fence_loaded": True,
        "provider_calls": 0,
        "source_and_copied_history_prefix_preserved": True,
        "copied_startup_appends": [row["type"] for row in appended],
        "handshakes": results,
        "readiness": "staged_verified_not_activated",
    }


if __name__ == "__main__":
    deployment, artifacts = (Path(arg).resolve() for arg in sys.argv[1:])
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deployment-handshake-", dir=artifacts) as fixture:
        receipt = asyncio.run(check(deployment, Path(fixture)))
    receipt["owned_fixtures_removed"] = True
    print(json.dumps(receipt, indent=2))
