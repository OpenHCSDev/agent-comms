"""Opt-in, pinned Pi fork executor with durable attempt-bound context evidence.

This proves Pi assembled an input in its model context, not provider receipt. It
never falls back to stock Pi, text matching, a command ACK, or in-memory entries.
No coordinator state changes or production runtime hookup occur in this module.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import stat
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .declarations import RelationViolationError, _store_lock
from .maintenance_barrier import MaintenanceBarrier
from .native_prompt_send import PromptSendUnknown, send_fenced_prompt
from .selected_tool_broker import (
    SelectedToolMode,
    SelectedToolSocket,
    stage_selected_extension,
    verify_selected_terminal,
)

if TYPE_CHECKING:
    from .fresh_private_session import FreshPrivateSession

CAPABILITY = "pi-native-input-v1-live-only"
_INPUT_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_LINE = 1 << 20
_MAX_JOURNAL = 16 << 20
# Every tracked launch must remove Pi session retry, provider transport retry,
# and overflow compaction-retry before an input can reach any provider.
_NATIVE_SETTINGS = (
    b'{"retry":{"enabled":false,"maxRetries":0,"provider":{"maxRetries":0}},'
    b'"compaction":{"enabled":false}}\n'
)


class NativePiUnavailable(RuntimeError):  # noqa: N818 - nominal fail-closed outcome
    """Tracked execution failed closed without committing a coordinator fact."""


def main() -> int:
    """Run the active route's pinned Pi for ordinary ACP owner sessions."""
    from .private_nk_entrypoint import private_nk_from_environment

    launch = private_nk_from_environment()
    if launch is None:
        raise NativePiUnavailable("Native owner backend requires a configured private route")
    cli = _trusted_package(launch.native_package)
    environment = dict(os.environ)
    environment["AGENT_COMMS_NATIVE_CONFIG_DIR"] = str(
        Path(
            environment.get("AGENT_COMMS_NATIVE_CONFIG_DIR")
            or environment.get("PI_CODING_AGENT_DIR")
            or "~/.pi/agent"
        )
        .expanduser()
        .resolve()
    )
    for name in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"):
        environment.pop(name, None)
    environment["NODE_DISABLE_COMPILE_CACHE"] = "1"
    os.execvpe(
        "node",
        [
            "node",
            "--no-global-search-paths",
            "--import",
            str(cli.with_name("agent-comms-import-fence.mjs")),
            "--import",
            str(cli.with_name("agent-comms-project-bootstrap.mjs")),
            str(cli),
            *sys.argv[1:],
        ],
        environment,
    )
    return 0


@dataclass(frozen=True, slots=True)
class NativeContextProof:
    input_id: str
    session_id: str
    session_entry_id: str
    request_generation: int
    llm_context_digest: str
    session_file: Path


class NativePiTerminalFailure(NativePiUnavailable):
    """A proved input ended in a failed terminal and its owned process was reaped."""

    def __init__(self, detail: str, context: NativeContextProof, provider: str, model: str):
        super().__init__(f"Native Pi assistant did not finish successfully: {detail}")
        self.context = context
        self.provider = provider
        self.model = model
        # Provider text may contain arbitrary response bodies. Only complete
        # known short errors may be broadcast into a shared channel.
        match = re.fullmatch(
            r"(?:(?:Codex|OpenAI|Anthropic) error: )?"
            r"(The usage limit has been reached|Insufficient credits|"
            r"(?:401|402|403|429)(?::\s*|\s+)(?:insufficient credits|rate limit|unauthorized)|"
            r"Model output limit reached)\.?",
            detail,
            flags=re.IGNORECASE,
        )
        self.public_message = (
            f"{provider}/{model}: {match.group(1) if match else 'provider request failed'}."
        )


@dataclass(frozen=True, slots=True)
class NativeTurnResult:
    text: str
    context: NativeContextProof
    selected_tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class NativePiRpcLaunch:
    """Verified native Pi subprocess launch, not an authorization to send input."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    session_dir: Path
    session_file: Path | None


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NativePiUnavailable("Ambiguous native Pi JSON object")
        result[key] = value
    return result


def _read_private_file(path: Path, *, max_bytes: int = _MAX_JOURNAL) -> list[dict[str, Any]]:
    try:
        info = path.lstat()
    except OSError as error:
        raise NativePiUnavailable("Native Pi evidence file is missing") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or not 0 < info.st_size <= max_bytes
    ):
        raise NativePiUnavailable("Native Pi evidence file is not private and bounded")
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes or not raw.endswith(b"\n"):
        raise NativePiUnavailable("Native Pi evidence file is incomplete")
    try:
        rows = [
            json.loads(line.decode("utf-8", errors="strict"), object_pairs_hook=_unique)
            for line in raw.split(b"\n")[:-1]
        ]
    except (UnicodeError, ValueError) as error:
        raise NativePiUnavailable("Native Pi evidence JSON is invalid") from error
    if any(type(row) is not dict for row in rows):
        raise NativePiUnavailable("Native Pi evidence row has wrong type")
    return rows


def _trusted_package(package: Path) -> Path:
    package = package.absolute()  # lexical: resolve() would hide a symlink component
    if ".." in package.parts:
        raise NativePiUnavailable("Pinned native Pi package path is not lexical")
    if not str(package).startswith("/var/tmp/agent-comms-pi-native-") or package.parts[-3:] != (
        "node_modules",
        "@earendil-works",
        "pi-coding-agent",
    ):
        raise NativePiUnavailable("Pinned disposable native Pi package is required")
    for ancestor in (package, *package.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise NativePiUnavailable("Native Pi package has a redirected ancestor")
        if info.st_uid not in (0, os.geteuid()):
            raise NativePiUnavailable("Native Pi package has a foreign ancestor")
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise NativePiUnavailable("Native Pi package has a writable ancestor")
    root = package.parents[2]
    if root.stat().st_uid != os.geteuid() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise NativePiUnavailable("Disposable native Pi root must be owner-only")
    from .native_package import NativePackageError, verify_native_package

    try:
        verify_native_package(package)
    except (OSError, NativePackageError) as error:
        raise NativePiUnavailable("Pinned native Pi package differs from reviewed fork") from error
    return package / "dist/cli.js"


def _private_session_dir(directory: Path) -> None:
    if ".." in directory.parts:
        raise NativePiUnavailable("Native Pi session directory is not lexical")
    for ancestor in (directory, *directory.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise NativePiUnavailable("Native Pi session directory has a redirected ancestor")
        if info.st_uid not in (0, os.geteuid()):
            raise NativePiUnavailable("Native Pi session directory has a foreign ancestor")
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise NativePiUnavailable("Native Pi session directory has a writable ancestor")
    info = directory.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise NativePiUnavailable("Native Pi session directory must be owner-only")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_private_session_dir(directory: Path) -> None:
    """Commit each new directory entry before a Pi launch can be returned.

    Re-sync the private ancestor chain even on reuse: a visible directory may
    have survived a failed parent fsync on the previous preparation. Stop at
    the parent of the highest owner-only directory, not at the filesystem root.
    """
    missing: list[Path] = []
    current = directory
    while True:
        try:
            current.lstat()
        except FileNotFoundError:
            missing.append(current)
            current = current.parent
        else:
            break
    try:
        for path in reversed(missing):
            path.mkdir(mode=0o700, exist_ok=True)
            _private_session_dir(path)
            _fsync_directory(path.parent)
        _private_session_dir(directory)
        parent = directory.parent
        while True:
            _fsync_directory(parent)
            info = parent.lstat()
            if (
                parent == parent.parent
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                break
            parent = parent.parent
    except OSError as error:
        raise NativePiUnavailable("Native Pi session directory could not be committed") from error


def _private_agent_dir(session_dir: Path) -> Path:
    """Durably isolate Pi settings from user/global and project retry policy.

    Refresh and fsync the exact policy for *every* attempt; a previously visible
    settings file or directory is not evidence that an earlier fsync succeeded.
    No credentials are copied into this directory (provider auth uses the env).
    """
    agent_dir = session_dir / ".native-pi-agent"
    try:
        agent_dir.mkdir(mode=0o700, exist_ok=True)
        _private_session_dir(agent_dir)
        _fsync_directory(session_dir)
        temporary = agent_dir / f".settings-{uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(_NATIVE_SETTINGS)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, agent_dir / "settings.json")
            _fsync_directory(agent_dir)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise NativePiUnavailable(
            "Native Pi private retry policy could not be committed"
        ) from error
    return agent_dir


def _session_location(directory: Path, candidate: str) -> Path:
    if not isinstance(candidate, str) or not candidate:
        raise NativePiUnavailable("Native Pi omitted its session file")
    path = Path(candidate)
    if not path.is_absolute() or path.parent != directory or path.suffix != ".jsonl":
        raise NativePiUnavailable("Native Pi changed its private session location")
    return path


def read_tracked_input_digest(session_file: Path, input_id: str) -> str:
    """Return the durable journal digest for one tracked user input.

    Corroboration only: this never proves the journal row fsynced, so callers
    must join it to an independently recorded live proof before trusting it.
    """
    if type(input_id) is not str or _INPUT_ID.fullmatch(input_id) is None:
        raise ValueError("A tracked input digest lookup requires a 128-bit input ID")
    session_file = Path(session_file).absolute()
    _private_session_dir(session_file.parent)
    entries = _read_private_file(session_file)
    if (
        not entries
        or entries[0].get("type") != "session"
        or type(entries[0].get("id")) is not str
        or not entries[0]["id"]
    ):
        raise NativePiUnavailable("Native Pi session header is invalid")
    observed: dict[str, str] = {}
    for entry in entries:
        message = entry.get("message")
        if entry.get("type") != "message" or not isinstance(message, dict):
            continue
        tracked_id = message.get("inputId")
        if tracked_id is None:
            continue
        digest = message.get("inputDigest")
        if (
            message.get("role") != "user"
            or type(tracked_id) is not str
            or _INPUT_ID.fullmatch(tracked_id) is None
            or type(digest) is not str
            or _DIGEST.fullmatch(digest) is None
            or tracked_id in observed
        ):
            raise NativePiUnavailable("Native Pi session has ambiguous tracked user input")
        observed[tracked_id] = digest
    if input_id not in observed:
        raise NativePiUnavailable("The specified input was never durably committed")
    return observed[input_id]


def _read_native_context_evidence(session_file: Path, input_id: str) -> NativeContextProof:
    """Parse a private journal only as corroboration of a live, emitted Pi event.

    The current copied Pi fork cannot distinguish a complete row whose fsync
    failed; this parser by itself MUST NOT authorize recovered context state.
    """
    if type(input_id) is not str or _INPUT_ID.fullmatch(input_id) is None:
        raise ValueError("A native context lookup requires a 128-bit input ID")
    session_file = Path(session_file).absolute()
    _private_session_dir(session_file.parent)
    entries = _read_private_file(session_file)
    if (
        not entries
        or entries[0].get("type") != "session"
        or type(entries[0].get("id")) is not str
        or not entries[0]["id"]
    ):
        raise NativePiUnavailable("Native Pi session header is invalid")
    session_id = entries[0]["id"]
    tracked: dict[str, str] = {}
    for entry in entries:
        message = entry.get("message")
        if entry.get("type") != "message" or not isinstance(message, dict):
            continue
        tracked_id = message.get("inputId")
        if tracked_id is None:
            continue
        digest = message.get("inputDigest")
        entry_id = entry.get("id")
        if (
            message.get("role") != "user"
            or type(tracked_id) is not str
            or _INPUT_ID.fullmatch(tracked_id) is None
            or type(digest) is not str
            or _DIGEST.fullmatch(digest) is None
            or type(entry_id) is not str
            or not entry_id
            or tracked_id in tracked
        ):
            raise NativePiUnavailable("Native Pi session has ambiguous tracked user input")
        tracked[tracked_id] = entry_id
    if input_id not in tracked:
        raise NativePiUnavailable("The specified input was never durably committed")
    journal = _read_private_file(Path(str(session_file) + ".input-proof"))
    expected_keys = {
        "schema",
        "type",
        "sessionId",
        "inputId",
        "sessionEntryId",
        "requestGeneration",
        "llmContextDigest",
    }
    previous_generation = 0
    generation_digest: str | None = None
    seen: set[tuple[int, str]] = set()
    chosen: dict[str, Any] | None = None
    for row in journal:
        generation = row.get("requestGeneration")
        digest = row.get("llmContextDigest")
        tracked_id = row.get("inputId")
        if (
            set(row) != expected_keys
            or type(row.get("schema")) is not int
            or row["schema"] != 1
            or row.get("type") != "context_committed"
            or row.get("sessionId") != session_id
            or type(generation) is not int
            or generation < 1
            or generation < previous_generation
            or type(digest) is not str
            or _DIGEST.fullmatch(digest) is None
            or type(tracked_id) is not str
            or tracked.get(tracked_id) != row.get("sessionEntryId")
            or (generation == previous_generation and digest != generation_digest)
            or (generation, tracked_id) in seen
        ):
            raise NativePiUnavailable("Native Pi proof journal contains an invalid row")
        if generation != previous_generation:
            generation_digest = digest
            previous_generation = generation
        seen.add((generation, tracked_id))
        if tracked_id == input_id:
            chosen = row
    if chosen is None:
        raise NativePiUnavailable("The input has no assembled-context proof")
    return NativeContextProof(
        input_id,
        session_id,
        tracked[input_id],
        chosen["requestGeneration"],
        chosen["llmContextDigest"],
        session_file,
    )


def load_native_context_proof(session_file: Path, input_id: str) -> NativeContextProof:
    """Fail closed: a persisted row alone cannot prove journal fsync succeeded.

    Until the native fork records a separate verified commit marker, no reboot
    reader may promote this evidence to CONTEXT_COMMITTED or retry the input.
    """
    raise NativePiUnavailable("Recovered native Pi context lacks a durable commit marker")


def _verify_context(
    session_file: Path,
    input_id: str,
    session_id: str,
    input_event: dict[str, Any],
    context_event: dict[str, Any],
) -> NativeContextProof:
    if (
        input_event.get("sessionId") != session_id
        or input_event.get("inputId") != input_id
        or context_event.get("sessionId") != session_id
        or context_event.get("inputId") != input_id
        or type(input_event.get("sessionEntryId")) is not str
        or context_event.get("sessionEntryId") != input_event["sessionEntryId"]
        or type(context_event.get("requestGeneration")) is not int
        or context_event["requestGeneration"] <= 0
        or type(context_event.get("llmContextDigest")) is not str
        or _DIGEST.fullmatch(context_event["llmContextDigest"]) is None
    ):
        raise NativePiUnavailable("Native Pi input/context events disagree")
    proof = _read_native_context_evidence(session_file, input_id)
    if (
        proof.session_id != session_id
        or proof.session_entry_id != input_event["sessionEntryId"]
        or proof.request_generation != context_event["requestGeneration"]
        or proof.llm_context_digest != context_event["llmContextDigest"]
    ):
        raise NativePiUnavailable("Native Pi emitted an event without matching durable proof")
    return proof


def _require_reviewed_selected_source_cli() -> None:
    """Default OFF until copied CLI startup factories are independently fenced."""
    raise NativePiUnavailable(
        "Selected first-source CLI builtins are unreviewed; no Pi startup or raw prompt"
    )


def _fresh_selected_revision(
    fresh: FreshPrivateSession, *, started: bool = False
) -> tuple[int, int, int, int, int]:
    """Exact saved inode+revision; not a provider or terminal receipt."""
    if started:
        return fresh.verify_selected_startup()
    fresh.verify_prewrite()
    info = fresh.path.lstat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def prepare_native_pi_rpc_launch(
    package: Path,
    *,
    worktree: Path,
    session_dir: Path,
    session_file: Path | None = None,
    provider: str = "openrouter",
    model: str = "z-ai/glm-5.3-flash",
    thinking_level: str | None = None,
    selected_thinking_level: str | None = None,
    selected_tool_mode: SelectedToolMode | None = None,
) -> NativePiRpcLaunch:
    """Verify compiled Pi bytes and commit private no-retry policy before spawning.

    A backend must explicitly consume this launch, not guess Pi from a basename
    or trust an RPC capability response from an arbitrary executable. This
    only establishes the executable and its settings; native input, context,
    and model-delivery proofs remain separate per-attempt observations.
    """
    if any(
        not isinstance(value, str)
        or not value.strip()
        or value.startswith("-")
        or any(character.isspace() for character in value)
        for value in (provider, model)
    ):
        raise NativePiUnavailable("Native Pi requires an explicit provider and model")
    if selected_thinking_level is not None and (
        type(selected_thinking_level) is not str
        or selected_thinking_level not in {"low", "high"}
        or session_file is None
    ):
        raise NativePiUnavailable("Selected launch requires a saved session and supported level")
    if selected_tool_mode is not None and type(selected_tool_mode) is not SelectedToolMode:
        raise NativePiUnavailable("Selected tool requires a trusted nominal mode")
    if selected_thinking_level is not None and selected_tool_mode is not None:
        raise NativePiUnavailable("Selected fresh source cannot launch a file tool")
    cli = _trusted_package(package)
    worktree = Path(worktree).absolute()
    session_dir = Path(session_dir).absolute()
    _durable_private_session_dir(session_dir)
    if not worktree.is_dir():
        raise NativePiUnavailable("Native Pi worktree is unavailable")
    if session_file is not None:
        session_file = _session_location(session_dir, str(session_file))
        entries = _read_private_file(session_file)
        if not entries or entries[0].get("type") != "session":
            raise NativePiUnavailable("Selected native source lacks a session header")
        marker = entries[0].get("agentCommsSelectedFresh")
        if (marker is not None or selected_thinking_level is not None) and (
            type(marker) is not dict
            or marker != {"schema": 1, "thinkingLevel": selected_thinking_level}
            or selected_thinking_level not in {"low", "high"}
        ):
            raise NativePiUnavailable(
                "Selected fresh source cannot reopen without exact first-start token"
            )
    agent_dir = _private_agent_dir(session_dir)
    extension = stage_selected_extension(session_dir) if selected_tool_mode is not None else None
    argv = [
        "node",
        str(cli),
        "--mode",
        "rpc",
        "--no-approve",
        *(
            ("--no-tools",)
            if extension is None
            else ("--no-builtin-tools", "--tools", "selected_claimed_write")
        ),
        "--no-extensions",
        "--no-skills",
        "--no-context-files",
        "--session-dir",
        str(session_dir),
        "--provider",
        provider,
        "--model",
        model,
    ]
    if selected_thinking_level is not None:
        argv.extend(("--thinking", selected_thinking_level, "--no-prompt-templates", "--no-themes"))
    if extension is not None:
        argv.extend(("-e", str(extension)))
    if session_file is not None:
        argv.extend(("--session", str(session_file)))
    if thinking_level is not None and selected_thinking_level is None:
        argv.extend(("--thinking", thinking_level))
    if selected_thinking_level is not None:
        # This selected-only candidate is still hard-denied before real spawn.
        # Do not hand a credential, proxy, hooks, or ambient provider settings
        # to even a future reviewed source CLI. The copied c1 gate needs its
        # explicit marker; PR94 must separately review this exact env contract.
        if os.name != "posix":
            raise NativePiUnavailable("Selected source requires reviewed POSIX isolation")
        import pwd

        username = pwd.getpwuid(os.geteuid()).pw_name
        env = {
            "HOME": str(agent_dir),
            "USER": username,
            "LOGNAME": username,
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "TMPDIR": str(session_dir),
            "PI_OFFLINE": "1",
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "AGENT_COMMS_SELECTED_SOURCE_COPY": "1",
        }
    else:
        env = os.environ.copy()
        for name in (
            "PI_AGENT_ID",
            "PI_PARENT_ID",
            "PI_AGENT_TAGS",
            "AGENT_COMMS_THREAD",
            "AGENT_COMMS_TAGS",
            "AGENT_COMMS_SELECTED_TOOL_SOCKET",
            "AGENT_COMMS_SELECTED_TOOL_TOKEN",
        ):
            env.pop(name, None)
        env["PI_OFFLINE"] = "1"
        # Canonical credentials/catalog remain separate from retry isolation.
        env["AGENT_COMMS_NATIVE_CONFIG_DIR"] = str(
            Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser().resolve()
        )
        env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    return NativePiRpcLaunch(tuple(argv), worktree, env, session_dir, session_file)


async def run_native_pi_turn(
    package: Path,
    *,
    input_id: str,
    prompt: str,
    worktree: Path,
    session_dir: Path,
    session_file: Path | None = None,
    provider: str = "openrouter",
    model: str = "z-ai/glm-5.3-flash",
    thinking_level: str | None = None,
    timeout: float = 90.0,
    prompt_send_boundary: Callable[..., AbstractContextManager[None]] | None = None,
    maintenance_root: Path | None = None,
    fresh_selected: FreshPrivateSession | None = None,
    selected_tool_mode: SelectedToolMode | None = None,
) -> NativeTurnResult:
    """One tracked real Pi RPC prompt in an isolated, persisted session.

    The caller owns disposition/publication and must never infer either from an
    input ACK. No automatic replay, fallback, tool launch, or coordinator writes.
    """
    if type(input_id) is not str or _INPUT_ID.fullmatch(input_id) is None:
        raise ValueError("A native turn requires a 128-bit lowercase hex input ID")
    if not prompt or not isinstance(prompt, str) or not 0 < timeout <= 300:
        raise ValueError("A native turn requires bounded prompt and deadline")
    selected_revision = None
    if fresh_selected is not None:
        from .fresh_private_session import FreshPrivateSession

        if (
            type(fresh_selected) is not FreshPrivateSession
            or session_file != fresh_selected.path
            or fresh_selected.selected_thinking_level not in {"low", "high"}
            or prompt_send_boundary is None
            or maintenance_root is None
        ):
            raise NativePiUnavailable("Selected first source requires enrolled locked prewrite")
        selected_revision = _fresh_selected_revision(fresh_selected)
        _require_reviewed_selected_source_cli()  # Must fail before real CLI spawn.
    launch = prepare_native_pi_rpc_launch(
        package,
        worktree=worktree,
        session_dir=session_dir,
        session_file=session_file,
        provider=provider,
        model=model,
        thinking_level=thinking_level,
        selected_thinking_level=(
            fresh_selected.selected_thinking_level if fresh_selected is not None else None
        ),
        selected_tool_mode=selected_tool_mode,
    )
    session_dir, session_file = launch.session_dir, launch.session_file
    tool_socket: SelectedToolSocket | None = None
    if selected_tool_mode is not None:
        tool_socket = SelectedToolSocket(
            session_dir, os.urandom(32).hex(), selected_tool_mode.action
        )
        await tool_socket.start()
        launch.env["AGENT_COMMS_SELECTED_TOOL_SOCKET"] = str(tool_socket.path)
        launch.env["AGENT_COMMS_SELECTED_TOOL_TOKEN"] = tool_socket.token
    try:
        process = await asyncio.create_subprocess_exec(
            *launch.argv,
            cwd=str(launch.cwd),
            env=launch.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_MAX_LINE + 1,
            start_new_session=True,
        )
    except BaseException:
        if tool_socket is not None:
            await tool_socket.close()
        raise
    if tool_socket is not None:
        tool_socket.expected_pid = process.pid
    stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
    assert stdin is not None and stdout is not None and stderr is not None
    stderr_task = asyncio.create_task(stderr.read(_MAX_LINE))
    deadline = asyncio.get_running_loop().time() + timeout

    async def next_event() -> dict[str, Any]:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise NativePiUnavailable("Native Pi turn deadline expired")
        raw = await asyncio.wait_for(stdout.readline(), timeout=remaining)
        if not raw or len(raw) > _MAX_LINE or not raw.endswith(b"\n"):
            raise NativePiUnavailable("Native Pi RPC record is incomplete")
        try:
            event = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique)
        except (UnicodeError, ValueError) as error:
            raise NativePiUnavailable("Native Pi RPC JSON is invalid") from error
        if type(event) is not dict:
            raise NativePiUnavailable("Native Pi RPC record has wrong type")
        return event

    async def send(command: dict[str, Any]) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise NativePiUnavailable("Native Pi send deadline expired")
        payload = (json.dumps(command, separators=(",", ":")) + "\n").encode()
        if maintenance_root is not None:
            # Only the non-provider capability preflight uses this buffered
            # writer. The tracked prompt has a separate one-use raw writer
            # holding wire→bus→registry→SQL authority through os.write.
            try:
                with _store_lock(maintenance_root / "wire"):
                    MaintenanceBarrier(maintenance_root / "registry.json").assert_open_unlocked()
                    stdin.write(payload)
            except RelationViolationError as error:
                raise NativePiUnavailable(
                    "Maintenance closed before Pi capability preflight"
                ) from error
        else:
            stdin.write(payload)
        await asyncio.wait_for(stdin.drain(), timeout=remaining)

    try:
        await send({"type": "get_state", "id": "native-capability"})
        while True:
            event = await next_event()
            if event.get("type") != "response" or event.get("id") != "native-capability":
                raise NativePiUnavailable("Native Pi emitted an unexpected preflight event")
            data = event.get("data")
            if (
                event.get("success") is not True
                or event.get("command") != "get_state"
                or type(data) is not dict
                or data.get("nativeInputProofCapability") != CAPABILITY
                or type(data.get("sessionId")) is not str
                or not data["sessionId"]
            ):
                raise NativePiUnavailable("Patched persisted Pi capability is unavailable")
            if type(data.get("sessionFile")) is not str:
                raise NativePiUnavailable("Native Pi omitted its private session file")
            actual_file = _session_location(session_dir, data["sessionFile"])
            if session_file is not None and actual_file != session_file:
                raise NativePiUnavailable("Native Pi rebound its session")
            session_id = data["sessionId"]
            if fresh_selected is not None:
                actual_model = data.get("model")
                if (
                    session_id != fresh_selected.session_id
                    or type(actual_model) is not dict
                    or actual_model.get("provider") != "openrouter"
                    or actual_model.get("id") != "z-ai/glm-5.3-flash"
                    or data.get("thinkingLevel") != fresh_selected.selected_thinking_level
                    or type(data.get("messageCount")) is not int
                    or data["messageCount"] != 0
                    or type(data.get("pendingMessageCount")) is not int
                    or data["pendingMessageCount"] != 0
                    or data.get("isStreaming") is not False
                    or data.get("isCompacting") is not False
                ):
                    raise NativePiUnavailable("Selected first source runtime or inode differs")
                startup_revision = _fresh_selected_revision(fresh_selected, started=True)
                if startup_revision[:2] != selected_revision[:2]:
                    raise NativePiUnavailable("Selected startup changed enrolled inode")
                selected_revision = startup_revision
            break
        command = {"type": "prompt", "id": "native-prompt", "inputId": input_id, "message": prompt}
        if prompt_send_boundary is None:
            await send(command)
        else:
            # A dedicated raw writer holds admission through every actual pipe
            # write, independent of owner-loop lifecycle callbacks and drain.
            # There is no buffered prompt remainder to flush after revocation.
            await send_fenced_prompt(
                stdin,
                (json.dumps(command, separators=(",", ":")) + "\n").encode(),
                (
                    (lambda: prompt_send_boundary(actual_file, selected_revision))
                    if fresh_selected is not None
                    else (lambda: prompt_send_boundary(actual_file))
                ),
                timeout=deadline - asyncio.get_running_loop().time(),
            )
        accepted = False
        input_event: dict[str, Any] | None = None
        contexts: list[dict[str, Any]] = []
        chunks: list[str] = []
        final_messages: list[str] = []
        terminal_error: str | None = None
        selected_call_id: str | None = None
        selected_args: object = None
        selected_started = False
        selected_finished = False
        while True:
            event = await next_event()
            kind = event.get("type")
            if kind == "response" and event.get("id") == "native-prompt":
                if accepted or event.get("command") != "prompt" or event.get("success") is not True:
                    raise NativePiUnavailable("Native Pi did not accept the tracked prompt")
                accepted = True
            elif kind == "input_committed" and event.get("inputId") == input_id:
                if input_event is not None:
                    raise NativePiUnavailable("Native Pi repeated the input commitment")
                input_event = event
            elif kind == "context_committed" and event.get("inputId") == input_id:
                contexts.append(event)
            elif kind == "message_update":
                delta = event.get("assistantMessageEvent")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = delta.get("delta")
                    if type(text) is not str:
                        raise NativePiUnavailable("Native Pi returned malformed model text")
                    chunks.append(text)
            elif kind == "message_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    content = message.get("content")
                    if message.get("errorMessage"):
                        terminal_error = str(message["errorMessage"])
                        continue
                    if not isinstance(content, list) or any(
                        not isinstance(item, dict) for item in content
                    ):
                        raise NativePiUnavailable("Native Pi assistant content is malformed")
                    if message.get("stopReason") == "toolUse" and tool_socket is not None:
                        calls = [item for item in content if item.get("type") == "toolCall"]
                        if (
                            selected_call_id is not None
                            or len(calls) != 1
                            or calls[0].get("name") != "selected_claimed_write"
                            or type(calls[0].get("id")) is not str
                            or any(
                                item.get("type") not in {"toolCall", "text", "thinking"}
                                for item in content
                            )
                        ):
                            raise NativePiUnavailable("Native Pi returned an unapproved tool call")
                        selected_call_id = calls[0]["id"]
                        selected_args = calls[0].get("arguments")
                        chunks.clear()  # Intermediate text is not the final response.
                    elif message.get("stopReason") == "stop":
                        if selected_call_id is not None and not selected_finished:
                            raise NativePiUnavailable(
                                "Native Pi returned text before tool terminal"
                            )
                        parts: list[str] = []
                        for item in content:
                            if item.get("type") == "text" and type(item.get("text")) is str:
                                parts.append(item["text"])
                            elif item.get("type") != "thinking":
                                raise NativePiUnavailable(
                                    "Native Pi assistant returned non-text content"
                                )
                        final_messages.append("".join(parts))
                    else:
                        terminal_error = (
                            "Model output limit reached"
                            if message.get("stopReason") == "length"
                            else "Provider returned an unsuccessful terminal"
                        )
            elif kind == "tool_execution_start":
                if (
                    tool_socket is None
                    or selected_started
                    or selected_call_id is None
                    or event.get("toolName") != "selected_claimed_write"
                    or event.get("toolCallId") != selected_call_id
                    or event.get("args") != selected_args
                ):
                    raise NativePiUnavailable("Native Pi began an unapproved tool execution")
                # A tool-time write cannot wait for final turn settlement, but
                # must not occur on mere get_state, prompt ACK, or a spoofed
                # tool event. The live input/context pair and durable journal
                # must already corroborate this exact tracked user input.
                if not accepted or input_event is None or not contexts:
                    raise NativePiUnavailable("Native Pi tool preceded tracked context proof")
                _verify_context(actual_file, input_id, session_id, input_event, contexts[-1])
                tool_socket.approve_tool_start(selected_call_id, event.get("args"))
                if selected_call_id not in tool_socket._approved:
                    raise NativePiUnavailable("Native Pi tool arguments are invalid")
                selected_started = True
            elif kind == "tool_execution_end":
                if (
                    tool_socket is None
                    or not selected_started
                    or selected_finished
                    or event.get("toolName") != "selected_claimed_write"
                    or event.get("toolCallId") != selected_call_id
                    or event.get("isError") is not False
                    or tool_socket.completed_call_id != selected_call_id
                ):
                    raise NativePiUnavailable("Native Pi selected tool did not finish successfully")
                try:
                    verify_selected_terminal(session_dir, input_id, selected_call_id)
                except (OSError, ValueError) as error:
                    raise NativePiUnavailable(
                        "Native Pi tool lacks an owner terminal receipt"
                    ) from error
                selected_finished = True
            elif kind == "agent_settled":
                break
            elif kind == "response" and event.get("command") in {
                "new_session",
                "switch_session",
                "fork",
            }:
                raise NativePiUnavailable("Native Pi session identity changed during a turn")
        if not accepted or input_event is None or not contexts:
            raise NativePiUnavailable("Native Pi did not commit a tracked model context")
        if terminal_error is not None:
            proof = _verify_context(actual_file, input_id, session_id, input_event, contexts[-1])
            raise NativePiTerminalFailure(terminal_error, proof, provider, model)
        if selected_call_id is not None and not selected_finished:
            raise NativePiUnavailable("Native Pi selected tool has no terminal result")
        if (
            len(final_messages) != 1
            or not final_messages[0]
            or "".join(chunks) != final_messages[0]
        ):
            raise NativePiUnavailable("Native Pi has no unique authoritative completed response")
        proof = _verify_context(actual_file, input_id, session_id, input_event, contexts[-1])
        return NativeTurnResult(final_messages[0].strip(), proof, selected_call_id)
    except PromptSendUnknown as error:
        raise NativePiUnavailable("Native Pi prompt send is UNKNOWN; no retry") from error
    except (TimeoutError, OSError) as error:
        raise NativePiUnavailable("Native Pi tracked turn failed; send may be UNKNOWN") from error
    finally:

        async def cleanup() -> None:
            stdin.close()
            if process.returncode is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except PermissionError:
                    # A just-exited macOS child may have lost its process group.
                    with suppress(ProcessLookupError):
                        process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                if process.returncode is None:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except PermissionError:
                        with suppress(ProcessLookupError):
                            process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            if tool_socket is not None:
                await tool_socket.close()

        # Repeated caller cancellation must not abandon the child or its reader.
        cleanup_task = asyncio.create_task(cleanup())
        cancelled_during_cleanup = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled_during_cleanup = True
        cleanup_task.result()
        if cancelled_during_cleanup:
            raise asyncio.CancelledError
