"""Explicit saved-session Pi /compact with no replay of uncertain attempts.

This is not a prompt and does not retry an uncertain response. It is deliberately
inert until called by an idle owner.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import pi_events as pi
from .backend import compaction_summary, configured_model, rpc_args_for
from .child_process import AttachedChild, Platform, ProcessGroups
from .native_session_reopen import NativeSessionIdentity
from .pi_commands import Compact, GetState, PiCommand
from .pi_helper import PiHelper, PiHelperError, SessionHelperRequest
from .pi_rpc import PiRpcChannel

MAX_LINE = 64 * 1024
MAX_OUTPUT = 256 * 1024
MAX_SESSION = 256 * 1024 * 1024
MAX_INSTRUCTIONS = 4096
TIMEOUT = 300.0
_POLICY = (
    b'{"retry":{"enabled":false,"maxRetries":0,"provider":{"maxRetries":0}},'
    b'"compaction":{"enabled":false}}\n'
)
# Installed Pi 0.85.1: session retry, provider retry, session reopen, RPC and CLI
# project-trust semantics. Changed bytes cannot select stock Pi.
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


def _private_policy(*, credentials_source: Path | None = None) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="agent-comms-compact-"))
    try:
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise OSError("profile is not private")
        _fsync(directory.parent)
        for name, content in (("settings.json", _POLICY),):
            fd = os.open(
                directory / name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        if credentials_source is not None:
            source_fd = os.open(credentials_source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                source_stat = os.fstat(source_fd)
                if (
                    not stat.S_ISREG(source_stat.st_mode)
                    or source_stat.st_uid != os.getuid()
                    or stat.S_IMODE(source_stat.st_mode) & 0o077
                    or not 0 < source_stat.st_size <= 1024 * 1024
                ):
                    raise OSError("Subscription credentials are not a private regular file")
                auth_fd = os.open(
                    directory / "auth.json",
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with (
                    os.fdopen(source_fd, "rb", closefd=False) as source,
                    os.fdopen(auth_fd, "wb") as auth,
                ):
                    shutil.copyfileobj(source, auth)
                    auth.flush()
                    os.fsync(auth.fileno())
            finally:
                os.close(source_fd)
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
                if seen_provider or value not in {"openrouter", "openai-codex"}:
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
    for index, line in enumerate(data.splitlines()):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError) as error:
            raise ValueError("Malformed saved session row") from error
        if not isinstance(row, dict) or not isinstance(row.get("type"), str):
            raise ValueError("Malformed saved session row")
        if index == 0 and (row.get("type") != "session" or row.get("version") != 3):
            raise ValueError("Unsupported saved session version")
    return data


def _durable_compaction_row(file: Path, before: bytes) -> None:
    """Verify and sync Pi's appended row and its directory before reporting success."""
    fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not 0 < opened.st_size <= MAX_SESSION:
            raise ValueError("Saved session is not a bounded regular file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            after = stream.read(MAX_SESSION + 1)
        if not after.endswith(b"\n") or not after.startswith(before) or after == before:
            raise ValueError("Missing saved compaction")
        try:
            added = [json.loads(line) for line in after[len(before) :].splitlines()]
        except (ValueError, UnicodeError) as error:
            raise ValueError("Malformed saved compaction rows") from error
        if (
            not added
            or not isinstance(added[-1], dict)
            or added[-1].get("type") != "compaction"
            or any(
                not isinstance(row, dict)
                or row.get("type") not in {"model_change", "thinking_level_change"}
                for row in added[:-1]
            )
        ):
            raise ValueError("Unexpected concurrent saved session write")
        os.fsync(fd)
        _fsync(file.parent)
        # Do not attest to a stale inode replaced while Pi or another writer ran.
        current = file.stat(follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (
            current.st_dev,
            current.st_ino,
            current.st_size,
        ) != (opened.st_dev, opened.st_ino, opened.st_size):
            raise ValueError("Saved session changed during durability check")
    finally:
        os.close(fd)


class ManualPreflightHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "manual_preflight.mjs"
    request = SessionHelperRequest
    result = NativeSessionIdentity


async def _preflight(
    package: Path, session: Path, cwd: Path, env: dict[str, str]
) -> NativeSessionIdentity | None:
    try:
        return await ManualPreflightHelper.run(
            SessionHelperRequest(str(package), str(session)), cwd=cwd, env=env
        )
    except PiHelperError:
        return None


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


def _public_pi_compaction_error(value: str | None) -> str:
    """Classify a Pi failure without copying provider text or prompt data to ACP."""
    raw = value or ""
    lower = raw.lower()
    if "generation hit the token cap" in lower or "summary is incomplete" in lower:
        return "Compaction summary hit the model output limit."
    if any(
        marker in lower
        for marker in (
            "maximum context length",
            "context length exceeded",
            "context_length_exceeded",
            "exceeds the context window",
            "prompt is too long",
        )
    ):
        return "Compaction summary exceeded the model context limit."
    # Pi formats a provider status as the leading code after this exact label.
    # A free-standing number may come from an echoed prompt or token count.
    status = re.match(r"^(?:Summarization|Turn prefix summarization) failed: ([45]\d\d):", raw)
    if status:
        return f"Compaction provider returned HTTP {status.group(1)}."
    return "Pi compaction failed; inspect local diagnostics."


class ManualCompaction:
    """One explicit saved-session transaction, owning its writer and cleanup.

    A transaction can run once. The ACP/TurnRunner boundary owns admission;
    this owner holds the existing session fence through durable result or
    uncertain teardown. Canonical PR95 sessions use their separate journal
    authority and cannot enter this direct-writer transaction.
    """

    def __init__(
        self,
        agent_bin: str,
        agent_args: Sequence[str],
        session_file: str,
        cwd: str,
        custom_instructions: str | None = None,
        *,
        timeout_seconds: float = TIMEOUT,
    ):
        self.agent_bin = agent_bin
        self.agent_args = tuple(agent_args)
        self.session_file = session_file
        self.cwd = cwd
        self.instructions = custom_instructions
        self.timeout = timeout_seconds
        self._used = False
        self.proc: AttachedChild | None = None
        self.drain: asyncio.Task[None] | None = None
        self.reader: PiRpcChannel | None = None
        self.profile: Path | None = None
        self.result: dict[str, Any] = {"ok": False, "error": "Compaction did not complete."}
        self.force = False
        self.cancelled = False
        self.clean = False

    def _validate(self) -> str | None:
        self.rpc_args = rpc_args_for(self.agent_bin, self.agent_args)
        if self.rpc_args is None:
            return "Compaction requires a Pi RPC backend."
        if not _safe_args(self.agent_args):
            return "Compaction arguments could override the saved session."
        selected = configured_model(self.agent_args)
        assert selected is not None and "/" in selected
        self.provider, self.model = selected.split("/", 1)
        if not isinstance(self.instructions, (str, type(None))) or (
            self.instructions is not None and len(self.instructions) > MAX_INSTRUCTIONS
        ):
            return "Compaction instructions are invalid or too long."
        try:
            self.session = Path(self.session_file).absolute()
            self.project = Path(self.cwd).absolute()
            self.before = _session_bytes(self.session)
            if not self.project.is_dir() or not (
                shutil.which(self.agent_bin) or Path(self.agent_bin).is_file()
            ):
                raise ValueError("Unavailable backend/project")
            self.package = _pinned_package()
        except (OSError, ValueError, TypeError):
            return "Saved session or pinned Pi backend is unavailable."
        return None

    def _prepare_profile(self) -> None:
        agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi/agent")
        auth = agent_dir / "auth.json"
        credentials = auth if auth.exists() or self.provider == "openai-codex" else None
        self.profile = _private_policy(credentials_source=credentials)
        self.env = dict(os.environ, PI_CODING_AGENT_DIR=str(self.profile))
        self.env.update(NODE_OPTIONS="", PI_OFFLINE="1", PI_TELEMETRY="0")

    async def run(self) -> dict[str, Any]:
        if self._used:
            raise RuntimeError("Manual compaction transaction already consumed; never replay")
        self._used = True
        if not isinstance(Platform.current(), ProcessGroups):
            return {"ok": False, "error": "Saved-session compaction requires POSIX teardown."}
        if not isinstance(self.timeout, (float, int)) or not 0 < self.timeout <= TIMEOUT:
            return {"ok": False, "error": "Compaction timeout is invalid."}
        from .session_fence import session_writer_fence

        try:
            async with asyncio.timeout(self.timeout):
                async with session_writer_fence(self.session_file):
                    return await self._execute()
        except TimeoutError:
            return {"ok": False, "error": "Compaction timed out; not retried."}
        except OSError:
            return {"ok": False, "error": "Saved session writer fence is unavailable."}

    async def _execute(self) -> dict[str, Any]:
        if error := self._validate():
            return {"ok": False, "error": error}
        try:
            self._prepare_profile()
        except OSError:
            return {"ok": False, "error": "Private no-retry policy could not be committed."}
        try:
            if await self._open_session():
                await self._compact()
        except asyncio.CancelledError:
            self.cancelled = self.force = True
        except (OSError, ValueError, UnicodeError, BrokenPipeError, ConnectionResetError):
            self.result = {"ok": False, "error": "Compaction transport or session check failed."}
            self.force = True
        finally:
            await self._close()
        if self.cancelled:
            raise asyncio.CancelledError
        if self.result.get("ok"):
            if not self.clean:
                return {"ok": False, "error": "Compaction process did not exit cleanly."}
            try:
                _durable_compaction_row(self.session, self.before)
            except (OSError, ValueError, UnicodeError, AttributeError):
                return {
                    "ok": False,
                    "error": "Saved compaction durability is uncertain; not retried.",
                }
        return self.result

    async def _open_session(self) -> bool:
        state = await _preflight(self.package, self.session, self.project, self.env)
        if not state or state.session_file != str(self.session) or not state.session_id:
            self.result = {"ok": False, "error": "Compaction requires a saved Pi session."}
            return False
        if _session_bytes(self.session) != self.before:
            self.result = {"ok": False, "error": "Saved session changed before compaction."}
            return False
        self.proc = await AttachedChild.start(
            (self.agent_bin, *self.rpc_args, "--session", str(self.session), *_FLAGS),
            cwd=self.project,
            env=self.env,
            limit=MAX_LINE,
        )
        assert self.proc.stdout is not None
        self.reader = PiRpcChannel(self.proc.stdout)
        self.drain = asyncio.create_task(self._discard_stderr())
        response = await self._request(GetState(id=uuid4().hex))
        data = response.data
        if (
            response.success is not True
            or data is None
            or data.session_file != str(self.session)
            or data.session_id != state.session_id
            or data.model is None
            or data.model.provider != self.provider
            or data.model.id != self.model
        ):
            raise ValueError("Pi reopened another session")
        current = _session_bytes(self.session)
        if not _startup_metadata(self.before, current):
            raise ValueError("Saved session changed during RPC startup")
        active = await _preflight(self.package, self.session, self.project, self.env)
        if (
            not active
            or active.session_file != str(self.session)
            or active.session_id != state.session_id
            or _session_bytes(self.session) != current
        ):
            raise ValueError("Pi active session changed before compaction")
        return True

    async def _discard_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while await self.proc.stderr.read(4096):
            pass

    async def _request(self, command: PiCommand) -> pi.Response:
        assert self.proc is not None and self.proc.stdin is not None and self.reader is not None
        self.proc.stdin.write(self.reader.encode(command))
        await self.proc.stdin.drain()
        consumed = 0
        while True:
            row = await self.reader.readline(max_bytes=MAX_OUTPUT - consumed)
            if not row:
                raise ValueError("No correlated response")
            consumed += len(row)
            if consumed > MAX_OUTPUT:
                raise ValueError("RPC output limit")
            response = self.reader.decode_record(row)
            if not isinstance(response, pi.Response) or response.id != command.id:
                continue
            if response.success is None or response.command is not type(command):
                raise ValueError("Invalid correlated response")
            if self.reader.correlate(response) is not command:
                raise ValueError("Unowned compaction response")
            return response

    async def _compact(self) -> None:
        response = await self._request(
            Compact(
                id=uuid4().hex,
                custom_instructions=(
                    (self.instructions.strip() or None) if self.instructions else None
                ),
            )
        )
        if not response.success:
            self.result = {"ok": False, "error": _public_pi_compaction_error(response.error)}
            return
        data = response.data
        if data is None:
            raise ValueError("Invalid compact data")
        self.result = {"ok": True, "summary": compaction_summary(data.summary)}
        for key, count in (
            ("tokensBefore", data.tokens_before),
            ("estimatedTokensAfter", data.estimated_tokens_after),
        ):
            if count is not None and count >= 0:
                self.result[key] = count

    async def _close(self) -> None:
        try:
            if self.proc is not None:
                completion = asyncio.create_task(
                    self.proc.stop() if self.force else self.proc.finish()
                )
                while not completion.done():
                    try:
                        await asyncio.shield(completion)
                    except asyncio.CancelledError:
                        self.cancelled = True
                self.clean = not self.force and completion.result().successful
                if self.drain is not None:
                    if not self.drain.done():
                        self.drain.cancel()
                    await asyncio.gather(self.drain, return_exceptions=True)
                if self.reader is not None:
                    self.reader.pending.cancel_all()
        finally:
            if self.profile is not None:
                shutil.rmtree(self.profile)
