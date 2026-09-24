"""Explicit saved-session Pi /compact, with a one-request provider budget.

This is not a prompt and does not retry an uncertain response. It is deliberately
inert until called by an idle owner. A Pi-version change fails closed until its
retry paths and split-turn summarization are re-reviewed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import stat
import tempfile
import unicodedata
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from .backend import rpc_args_for

MAX_LINE = 64 * 1024
MAX_OUTPUT = 256 * 1024
MAX_SESSION = 32 * 1024 * 1024
MAX_INSTRUCTIONS = 4096
TIMEOUT = 300.0
GRACE = 1.0
# Default keepRecentTokens is reproduced exactly in the independent Pi preflight.
_POLICY = (
    b'{"retry":{"enabled":false,"maxRetries":0,"provider":{"maxRetries":0}},'
    b'"compaction":{"enabled":false,"reserveTokens":16384,"keepRecentTokens":20000}}\n'
)
# A redirect is another POST even with both Pi retries disabled. Preload into
# the Pi Node process before its SDK imports; bound every POST and refuse HTTP
# 307/308 replay. Pi's HTTP dispatcher replaces global fetch via undici.install;
# retain the guard while accepting that implementation's fetch as the delegate.
_FETCH_GUARD = b"""let delegate = globalThis.fetch;
let posts = 0;
const guardedFetch = (input, init) => {
  const method = (init?.method ?? input?.method ?? 'GET').toUpperCase();
  if (method !== 'POST') return delegate(input, init);
  if (++posts > 1) throw new Error('Manual compact POST budget exhausted');
  return delegate(input, { ...init, redirect: 'manual' });
};
Object.defineProperty(globalThis, 'fetch', {
  configurable: false, enumerable: true,
  get: () => guardedFetch,
  set: value => {
    if (typeof value !== 'function') throw new Error('Manual compact fetch changed');
    delegate = value;
  },
});
"""
# Installed Pi 0.85.1: session retry, provider retry, session reopen, RPC and CLI
# project-trust semantics. No stock-Pi fallback on changed bytes.
_PINNED = {
    "dist/core/compaction/compaction.js": (
        "3d5f1f2a3e801c965214717b6abad1839239b4a030517bffdf0c8eff25df5c2a"
    ),
    "dist/core/settings-manager.js": (
        "ee4f52d1dd4f1c18d5d814be4ba260ddf7fe40b7b70c2f0732a30a8b287111ad"
    ),
    "dist/core/agent-session.js": (
        "fb8a3981c20c8c0bbd42231b1c99a10335fb3858b659056b341954de9cfa467f"
    ),
    "dist/core/session-manager.js": (
        "ccace64949db25379a43971ecea750c1b7ec6344e1bc31b9d5fe596ac2f1c9f3"
    ),
    "dist/core/sdk.js": "6969bd56ba8e1628cd033bb15cb15fe38299f00b5ad84f4f8ef37a33a98681c9",
    "dist/core/http-dispatcher.js": (
        "f9aa2c81b0a5958ffba6368506f24c9b202904c234ada19494cdc9c4a1d0e97b"
    ),
    "node_modules/undici/index.js": (
        "93fc85c00a59b58f410c901e5477d56770de3816bfbe183e751624aeb4a5e6a5"
    ),
    "node_modules/openai/client.mjs": (
        "45b54e0c0a779a8b284cc8bcfb41a0a0f1817eac32d9cc585ff71875b62aeb6b"
    ),
    "node_modules/openai/internal/shims.mjs": (
        "0eeb939ee18f8df00273d819ffe94d84fa2c3e990485902c4c94a108669ffaea"
    ),
    "dist/cli.js": "8189b66abc4f9f431dbb70941dcba690d76d040de1fbfff212886be35a53639d",
    "dist/config.js": "53098afc9f4bfa2a3720e6971561291b3850e78e9a22e482ffb4584bd4f123f8",
    "node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js": (
        "1e2097ced37cf0e21aa5711297eecc77916de8a4ed81a9019bc7d97b22825fa3"
    ),
    "dist/cli/args.js": "bfb311d2c5d919fa4015d6aaa3c5a71a90b90011320e5e40f56b12e448c44dfc",
    "dist/modes/rpc/rpc-mode.js": (
        "e7e4724aa55c5aac73cf36793653b26736200e5c59d58373990fc31028f86477"
    ),
    "node_modules/@earendil-works/pi-ai/dist/utils/retry.js": (
        "9e344f7662b334de0cbc7e7eccf0faa5f3c645a50b5fd8383495df700841f81e"
    ),
    "node_modules/@earendil-works/pi-ai/dist/utils/provider-retry.js": (
        "f14a8cfb9a99901a7cde6d588fad11aa8030e409d760d76050e580b6dba5c3f5"
    ),
}
_PACKAGE = Path.home() / ".local/pi-npm/lib/node_modules/@earendil-works/pi-coding-agent"
_FLAGS = (
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-context-files",
    "--no-approve",
    "--no-tools",
)
# No arbitrary CLI args: --session/--no-session, --approve, --mode, --,
# --extension and positional prompts must never override this invocation.
_VALUE_FLAGS = frozenset({"--provider", "--model", "--api-key", "--thinking"})
_SWITCH_FLAGS = frozenset({"--print"})
_PREFLIGHT = r"""
import { pathToFileURL } from 'node:url';
const root = process.env.COMPACT_PI_PACKAGE;
const { SessionManager } = await import(pathToFileURL(root + '/dist/core/session-manager.js'));
const { prepareCompaction } = await import(
  pathToFileURL(root + '/dist/core/compaction/compaction.js'));
const session = SessionManager.open(
  process.env.COMPACT_SESSION, undefined, process.env.COMPACT_CWD);
const preparation = prepareCompaction(session.getBranch(), {
  enabled: false, reserveTokens: 16384, keepRecentTokens: 20000
});
console.log(JSON.stringify({
  sessionId: session.getSessionId(), sessionFile: session.getSessionFile(),
  safe: !!preparation && !preparation.isSplitTurn,
}));
"""


def _pinned_package() -> Path:
    root = _PACKAGE
    try:
        for relative, expected in _PINNED.items():
            path = root / relative
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Pi version changed")
    except OSError as error:
        raise ValueError("Pinned Pi package missing") from error
    return root


def _private_policy() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="agent-comms-compact-"))
    try:
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise OSError("profile is not private")
        _fsync(directory.parent)
        for name, content in (("settings.json", _POLICY), ("guard.mjs", _FETCH_GUARD)):
            fd = os.open(
                directory / name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        _fsync(directory)
        return directory
    except BaseException:
        shutil.rmtree(directory)
        raise


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_args(args: Sequence[str]) -> bool:
    i = 0
    seen_provider = seen_model = False
    while i < len(args):
        arg = args[i]
        if not isinstance(arg, str):
            return False
        if arg in _SWITCH_FLAGS:
            i += 1
        elif arg in _VALUE_FLAGS and i + 1 < len(args):
            value = args[i + 1]
            if not isinstance(value, str) or not value or value.startswith("-") or "\x00" in value:
                return False
            if arg == "--provider":
                if seen_provider or value != "openrouter":
                    return False
                seen_provider = True
            if arg == "--model":
                if seen_model:
                    return False
                seen_model = True
            i += 2
        else:
            return False
    return seen_provider and seen_model


def _session_bytes(file: Path) -> bytes:
    if not file.is_file() or file.is_symlink() or not 0 < file.stat().st_size <= MAX_SESSION:
        raise ValueError("Missing or oversized saved session")
    with file.open("rb") as stream:
        data = stream.read(MAX_SESSION + 1)
    if len(data) > MAX_SESSION or not data.endswith(b"\n"):
        raise ValueError("Incomplete saved session")
    return data


def _supported_platform() -> bool:
    """Manual Pi compaction requires POSIX process-group teardown."""
    return os.name == "posix" and hasattr(os, "killpg")


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _signal_group(pid: int, sig: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


async def _shutdown(proc: asyncio.subprocess.Process, *, force: bool) -> bool:
    if proc.stdin is not None and not proc.stdin.is_closing():
        # Pi can close its read end before teardown. A broken pipe must not
        # skip the process-group signal and child wait.
        with suppress(BrokenPipeError, ConnectionResetError, OSError):
            proc.stdin.close()
    if force:
        _signal_group(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), GRACE)
    except TimeoutError:
        force = True
        _signal_group(proc.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(proc.wait(), GRACE)
        except TimeoutError:
            return False
    deadline = asyncio.get_running_loop().time() + GRACE
    if _group_alive(proc.pid):
        force = True
        _signal_group(proc.pid, signal.SIGTERM)
    while _group_alive(proc.pid):
        if asyncio.get_running_loop().time() >= deadline:
            _signal_group(proc.pid, signal.SIGKILL)
            return False
        await asyncio.sleep(0.02)
    return not force and proc.returncode == 0


async def _reap_immune(proc: asyncio.subprocess.Process, *, force: bool) -> tuple[bool, bool]:
    """Finish shutdown even under repeated caller cancellation."""
    cleanup = asyncio.create_task(_shutdown(proc, force=force))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    return cleanup.result(), cancelled


async def _preflight(
    package: Path, session: Path, cwd: Path, env: dict[str, str]
) -> dict[str, Any] | None:
    child_env = dict(
        env, COMPACT_PI_PACKAGE=str(package), COMPACT_SESSION=str(session), COMPACT_CWD=str(cwd)
    )
    proc = await asyncio.create_subprocess_exec(
        "node",
        "--input-type=module",
        "-e",
        _PREFLIGHT,
        cwd=cwd,
        env=child_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    output = b""
    force = False
    cancelled = False
    try:
        async with asyncio.timeout(10):
            assert proc.stdout is not None
            output = await proc.stdout.read(MAX_LINE + 1)
            if len(output) > MAX_LINE:
                force = True
    except TimeoutError:
        force = True
    except asyncio.CancelledError:
        force = True
        cancelled = True
    finally:
        clean, interrupted = await _reap_immune(proc, force=force)
    if cancelled or interrupted:
        raise asyncio.CancelledError
    if not clean or not output.endswith(b"\n"):
        return None
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError):
        return None
    return result if isinstance(result, dict) else None


async def _response(
    proc: asyncio.subprocess.Process, command: str, request_id: str
) -> dict[str, Any]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write((json.dumps({"id": request_id, "type": command}) + "\n").encode())
    await proc.stdin.drain()
    total = 0
    while True:
        line = await proc.stdout.readline()
        if not line:
            raise ValueError("No correlated response")
        total += len(line)
        if total > MAX_OUTPUT:
            raise ValueError("RPC output limit")
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError("Invalid RPC row")
        if payload.get("type") != "response" or payload.get("id") != request_id:
            continue
        if payload.get("command") != command or type(payload.get("success")) is not bool:
            raise ValueError("Invalid correlated response")
        return payload


def _startup_metadata(before: bytes, after: bytes) -> bool:
    """Pi may append model/thinking metadata on RPC reopen, never a message."""
    if not after.startswith(before):
        return False
    try:
        return all(
            json.loads(line).get("type") in {"model_change", "thinking_level_change"}
            for line in after[len(before) :].splitlines()
        )
    except (ValueError, AttributeError, UnicodeError):
        return False


def _summary(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    safe = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
    return " ".join(safe.split())[:1000]


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


async def compact_session(
    agent_bin: str,
    agent_args: Sequence[str],
    session_file: str,
    cwd: str,
    custom_instructions: str | None = None,
    *,
    timeout_seconds: float = TIMEOUT,
) -> dict[str, Any]:
    """Run a single explicit compact on an existing Pi session; never auto-retry."""
    if not _supported_platform():
        return {"ok": False, "error": "Saved-session compaction requires POSIX teardown."}
    rpc_args = rpc_args_for(agent_bin, agent_args)
    if rpc_args is None:
        return {"ok": False, "error": "Compaction requires a Pi RPC backend."}
    if not _safe_args(agent_args):
        return {"ok": False, "error": "Compaction arguments could override the saved session."}
    if not isinstance(custom_instructions, (str, type(None))) or (
        custom_instructions is not None and len(custom_instructions) > MAX_INSTRUCTIONS
    ):
        return {"ok": False, "error": "Compaction instructions are invalid or too long."}
    if not isinstance(timeout_seconds, (float, int)) or not 0 < timeout_seconds <= TIMEOUT:
        return {"ok": False, "error": "Compaction timeout is invalid."}
    try:
        session = Path(session_file).absolute()
        project = Path(cwd).absolute()
        before = _session_bytes(session)
        if not project.is_dir() or not (shutil.which(agent_bin) or Path(agent_bin).is_file()):
            raise ValueError("Unavailable backend/project")
        package = _pinned_package()
    except (OSError, ValueError, TypeError):
        return {"ok": False, "error": "Saved session or pinned Pi backend is unavailable."}
    try:
        profile = _private_policy()  # no child until both retry layers are fsynced
    except OSError:
        return {"ok": False, "error": "Private no-retry policy could not be committed."}
    env = os.environ.copy()
    env["PI_CODING_AGENT_DIR"] = str(profile)
    # Override inherited NODE_OPTIONS; no user-provided preloads can bypass
    # the hard single-POST guard or alter the effective retry policy.
    env["NODE_OPTIONS"] = f"--import={(profile / 'guard.mjs').as_uri()}"
    env["PI_OFFLINE"] = "1"
    env["PI_TELEMETRY"] = "0"
    proc: asyncio.subprocess.Process | None = None
    drain: asyncio.Task[None] | None = None
    result: dict[str, Any] = {"ok": False, "error": "Compaction did not complete."}
    force = False
    cancelled = False
    clean = False
    try:
        async with asyncio.timeout(timeout_seconds):
            state = await _preflight(package, session, project, env)
            if (
                not state
                or state.get("safe") is not True
                or state.get("sessionFile") != str(session)
                or type(state.get("sessionId")) is not str
                or not state["sessionId"]
            ):
                return {"ok": False, "error": "Compaction requires a non-split saved Pi session."}
            if _session_bytes(session) != before:
                return {"ok": False, "error": "Saved session changed before compaction."}
            proc = await asyncio.create_subprocess_exec(
                agent_bin,
                *rpc_args,
                "--session",
                str(session),
                *_FLAGS,
                cwd=project,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_LINE,
                start_new_session=True,
            )
            stderr = proc.stderr
            assert stderr is not None

            async def discard_stderr() -> None:
                while await stderr.read(4096):
                    pass

            drain = asyncio.create_task(discard_stderr())
            state_reply = await _response(proc, "get_state", uuid4().hex)
            data = state_reply.get("data")
            if (
                state_reply["success"] is not True
                or not isinstance(data, dict)
                or data.get("sessionFile") != str(session)
                or data.get("sessionId") != state["sessionId"]
                or not isinstance(data.get("model"), dict)
                or data["model"].get("provider") != "openrouter"
                or data["model"].get("api") != "openai-completions"
            ):
                raise ValueError("Pi reopened another session")
            current = _session_bytes(session)
            if not _startup_metadata(before, current):
                raise ValueError("Saved session changed during RPC startup")
            # Re-evaluate the exact branch *after* Pi's startup metadata writes.
            # A split turn could otherwise consume two provider calls even when
            # both retry layers are disabled.
            active = await _preflight(package, session, project, env)
            if (
                not active
                or active.get("safe") is not True
                or active.get("sessionFile") != str(session)
                or active.get("sessionId") != state["sessionId"]
                or _session_bytes(session) != current
            ):
                raise ValueError("Pi active session is not one-request safe")
            request_id = uuid4().hex
            command: dict[str, Any] = {"id": request_id, "type": "compact"}
            if custom_instructions and custom_instructions.strip():
                command["customInstructions"] = custom_instructions.strip()
            assert proc.stdin is not None
            proc.stdin.write((json.dumps(command) + "\n").encode())
            await proc.stdin.drain()
            # Read the exact compact id; unrelated events and responses are not success.
            assert proc.stdout is not None
            consumed = 0
            while True:
                row = await proc.stdout.readline()
                if not row:
                    raise ValueError("No compact response")
                consumed += len(row)
                if consumed > MAX_OUTPUT:
                    raise ValueError("Compact response limit")
                payload = json.loads(row)
                if not isinstance(payload, dict):
                    raise ValueError("Invalid compact response")
                if payload.get("type") != "response" or payload.get("id") != request_id:
                    continue
                if payload.get("command") != "compact" or type(payload.get("success")) is not bool:
                    raise ValueError("Invalid compact reply")
                if payload["success"] is not True:
                    result = {
                        "ok": False,
                        "error": "Pi compaction failed (local diagnostics only).",
                    }
                    break
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise ValueError("Invalid compact data")
                result = {"ok": True, "summary": _summary(data.get("summary"))}
                for key in ("tokensBefore", "estimatedTokensAfter"):
                    count = _count(data.get(key))
                    if count is not None:
                        result[key] = count
                break
    except TimeoutError:
        result, force = {"ok": False, "error": "Compaction timed out; not retried."}, True
    except asyncio.CancelledError:
        cancelled, force = True, True
    except (OSError, ValueError, UnicodeError, BrokenPipeError, ConnectionResetError):
        result, force = (
            {"ok": False, "error": "Compaction transport or session check failed."},
            True,
        )
    finally:
        try:
            if proc is not None:
                clean, interrupted = await _reap_immune(proc, force=force)
                cancelled |= interrupted
                if drain is not None:
                    if not drain.done():
                        drain.cancel()
                    await asyncio.gather(drain, return_exceptions=True)
        finally:
            shutil.rmtree(profile)
    if cancelled:
        raise asyncio.CancelledError
    if not clean and result.get("ok"):
        return {"ok": False, "error": "Compaction process did not exit cleanly."}
    if result.get("ok"):
        try:
            after = _session_bytes(session)
            if (
                not after.startswith(before)
                or after == before
                or json.loads(after.splitlines()[-1]).get("type") != "compaction"
            ):
                raise ValueError("Missing saved compaction")
        except (OSError, ValueError, UnicodeError, AttributeError):
            return {"ok": False, "error": "Compaction did not persist to the saved session."}
    return result
