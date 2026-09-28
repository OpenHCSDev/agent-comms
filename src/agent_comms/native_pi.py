"""Pinned Pi RPC execution with durable attempt-bound context evidence.

Native proof records the model context; the coordinator owns disposition and
publication. Verified package and send authority remain required for every turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import sys
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from . import pi_commands as commands
from . import pi_events as pi
from .child_process import BoundedRun
from .errors import RelationViolationError
from .maintenance_barrier import MaintenanceBarrier
from .native_entries import NativeEntry
from .native_prompt_send import PromptSendUnknown, send_fenced_prompt
from .native_tool_call import SelectedToolDenied
from .pi_payloads import TextDelta
from .pi_rpc import PiRpcChannel
from .selected_tool_broker import NativeToolMode, OwnerToolSocket
from .store_files import _store_lock

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
    # Global extensions invoke this installation's console tools. Services
    # need not inherit an activated virtualenv or an interactive shell PATH.
    environment["PATH"] = os.pathsep.join(
        (str(Path(sys.executable).parent), environment.get("PATH", os.defpath))
    )
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
    input_id: str = field(metadata={"wire_name": "inputId"})
    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_entry_id: str = field(metadata={"wire_name": "sessionEntryId"})
    request_generation: int = field(metadata={"wire_name": "requestGeneration"})
    llm_context_digest: str = field(metadata={"wire_name": "llmContextDigest"})
    session_file: Path

    @classmethod
    def from_journal(cls, row: dict, session_file: Path) -> NativeContextProof:
        """Decode only the declared strict journal envelope; never grant acceptance."""
        from .field_codec import FieldCodec

        names = {
            item.metadata.get("wire_name", item.name)
            for item in fields(cls)
            if item.name != "session_file"
        }
        if (
            set(row) != names | {"schema", "type"}
            or type(row["schema"]) is not int
            or row["schema"] != 1
            or row["type"] != "context_committed"
        ):
            raise ValueError("Native Pi proof journal contains an invalid row")
        hints = FieldCodec._types(cls)
        proof = cls(
            **{
                item.name: FieldCodec.decode(
                    hints[item.name], row[item.metadata.get("wire_name", item.name)]
                )
                for item in fields(cls)
                if item.name != "session_file"
            },
            session_file=session_file,
        )
        if (
            proof.request_generation < 1
            or not _INPUT_ID.fullmatch(proof.input_id)
            or not _DIGEST.fullmatch(proof.llm_context_digest)
        ):
            raise ValueError("Native Pi proof journal contains an invalid row")
        return proof

    @classmethod
    def read_evidence(
        cls, session_file: Path, input_id: str, *, request_generation: int | None = None
    ) -> NativeContextProof:
        """Corroborate live recorded events; parsed bytes alone grant no authority."""
        if type(input_id) is not str or _INPUT_ID.fullmatch(input_id) is None:
            raise ValueError("A native context lookup requires a 128-bit input ID")
        session_file = Path(session_file).absolute()
        header, entries = NativeEntry.read_evidence(session_file)
        tracked = NativeEntry.tracked_users(entries)
        if input_id not in tracked:
            raise NativePiUnavailable("The specified input was never durably committed")
        previous_generation = 0
        generation_digest = None
        seen = set()
        chosen = None
        for row in _read_private_file(Path(str(session_file) + ".input-proof")):
            try:
                proof = cls.from_journal(row, session_file)
                entry = tracked.get(proof.input_id)
                if (
                    proof.session_id != header.id
                    or entry is None
                    or proof.session_entry_id != entry.id
                    or proof.request_generation < previous_generation
                    or (
                        proof.request_generation == previous_generation
                        and proof.llm_context_digest != generation_digest
                    )
                    or (proof.request_generation, proof.input_id) in seen
                ):
                    raise ValueError("Native Pi proof journal contains an invalid row")
            except (ValueError, TypeError, KeyError) as error:
                raise NativePiUnavailable(
                    "Native Pi proof journal contains an invalid row"
                ) from error
            previous_generation = proof.request_generation
            generation_digest = proof.llm_context_digest
            seen.add((proof.request_generation, proof.input_id))
            if proof.input_id == input_id and (
                request_generation is None or proof.request_generation == request_generation
            ):
                chosen = proof
        if chosen is None:
            raise NativePiUnavailable("The input has no assembled-context proof")
        return chosen


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
    """Corroborating digest only; this cannot authorize recovery or input replay."""
    if type(input_id) is not str or _INPUT_ID.fullmatch(input_id) is None:
        raise ValueError("A tracked input digest lookup requires a 128-bit input ID")
    _header, entries = NativeEntry.read_evidence(Path(session_file).absolute())
    users = NativeEntry.tracked_users(entries)
    if input_id not in users:
        raise NativePiUnavailable("The specified input was never durably committed")
    return users[input_id].message.input_digest


def _verify_context(
    session_file: Path,
    input_id: str,
    session_id: str,
    input_event: pi.InputCommitted,
    context_event: pi.ContextCommitted,
) -> NativeContextProof:
    if (
        input_event.session_id != session_id
        or input_event.input_id != input_id
        or context_event.session_id != session_id
        or context_event.input_id != input_id
        or input_event.session_entry_id is None
        or context_event.session_entry_id != input_event.session_entry_id
        or context_event.request_generation is None
        or context_event.request_generation <= 0
        or context_event.llm_context_digest is None
        or _DIGEST.fullmatch(context_event.llm_context_digest) is None
    ):
        raise NativePiUnavailable("Native Pi input/context events disagree")
    proof = NativeContextProof.read_evidence(session_file, input_id)
    if (
        proof.session_id != session_id
        or proof.session_entry_id != input_event.session_entry_id
        or proof.request_generation != context_event.request_generation
        or proof.llm_context_digest != context_event.llm_context_digest
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
    selected_tool_mode: NativeToolMode | None = None,
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
    if selected_tool_mode is not None and not isinstance(selected_tool_mode, NativeToolMode):
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
        from .fresh_private_session import FreshPrivateSession

        FreshPrivateSession.require_launch_header(session_file, selected_thinking_level)
    agent_dir = _private_agent_dir(session_dir)
    tool_arguments = (
        selected_tool_mode.launch_arguments(package)
        if selected_tool_mode is not None
        else ("--no-tools",)
    )
    argv = [
        "node",
        str(cli),
        "--mode",
        "rpc",
        "--no-approve",
        *tool_arguments,
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
    selected_tool_mode: NativeToolMode | None = None,
    observe_event: Callable[[pi.PiEvent], Awaitable[None]] | None = None,
) -> NativeTurnResult:
    """One tracked real Pi RPC prompt in an isolated, persisted session.

    The caller owns disposition/publication and must never infer either from an
    input ACK. No automatic replay, unauthorized tool launch, or coordinator writes.
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
    tool_socket: OwnerToolSocket | None = None
    if selected_tool_mode is not None:
        tool_socket = selected_tool_mode.socket(session_dir, os.urandom(32).hex())
        await tool_socket.start()
        launch.env["AGENT_COMMS_SELECTED_TOOL_SOCKET"] = str(tool_socket.path)
        launch.env["AGENT_COMMS_SELECTED_TOOL_TOKEN"] = tool_socket.token
    try:
        async with BoundedRun.session(
            launch.argv, timeout=timeout, cwd=launch.cwd, env=launch.env, limit=_MAX_LINE + 1
        ) as process:
            if tool_socket is not None:
                tool_socket.expected_pid = process.pid
            stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
            assert stdin is not None and stdout is not None and stderr is not None
            stderr_task = asyncio.create_task(stderr.read(_MAX_LINE))
            deadline = asyncio.get_running_loop().time() + timeout

            channel = PiRpcChannel(stdout)

            async def next_event() -> pi.PiEvent:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise NativePiUnavailable("Native Pi turn deadline expired")
                try:
                    raw = await asyncio.wait_for(
                        channel.readline(max_bytes=_MAX_LINE), timeout=remaining
                    )
                except ValueError as error:
                    raise NativePiUnavailable("Native Pi RPC record is incomplete") from error
                if not raw or len(raw) > _MAX_LINE or not raw.endswith(b"\n"):
                    raise NativePiUnavailable("Native Pi RPC record is incomplete")
                try:
                    event = PiRpcChannel.decode_record(raw, strict=True, max_bytes=_MAX_LINE)
                except (UnicodeError, ValueError, TypeError) as error:
                    raise NativePiUnavailable("Native Pi RPC JSON is invalid") from error
                return event

            async def send(command: commands.PiCommand) -> None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise NativePiUnavailable("Native Pi send deadline expired")
                payload = channel.encode(command)
                if maintenance_root is not None:
                    # Only the non-provider capability preflight uses this buffered
                    # writer. The tracked prompt has a separate one-use raw writer
                    # holding wire→bus→registry→SQL authority through os.write.
                    try:
                        with _store_lock(maintenance_root / "wire"):
                            MaintenanceBarrier(
                                maintenance_root / "registry.json"
                            ).assert_open_unlocked()
                            stdin.write(payload)
                    except RelationViolationError as error:
                        raise NativePiUnavailable(
                            "Maintenance closed before Pi capability preflight"
                        ) from error
                else:
                    stdin.write(payload)
                await asyncio.wait_for(stdin.drain(), timeout=remaining)

            try:
                await send(commands.GetState(id="native-capability"))
                while True:
                    event = await next_event()
                    if not isinstance(event, pi.Response) or event.id != "native-capability":
                        raise NativePiUnavailable("Native Pi emitted an unexpected preflight event")
                    data = event.data
                    if (
                        event.success is not True
                        or event.command is not commands.GetState
                        or data is None
                        or data.native_input_proof_capability != CAPABILITY
                        or data.session_id is None
                        or not data.session_id
                    ):
                        raise NativePiUnavailable("Patched persisted Pi capability is unavailable")
                    if data.session_file is None:
                        raise NativePiUnavailable("Native Pi omitted its private session file")
                    actual_file = _session_location(session_dir, data.session_file)
                    if session_file is not None and actual_file != session_file:
                        raise NativePiUnavailable("Native Pi rebound its session")
                    session_id = data.session_id
                    if fresh_selected is not None:
                        actual_model = data.model
                        if (
                            session_id != fresh_selected.session_id
                            or actual_model is None
                            or actual_model.provider != "openrouter"
                            or actual_model.id != "z-ai/glm-5.3-flash"
                            or data.thinking_level != fresh_selected.selected_thinking_level
                            or data.message_count != 0
                            or data.pending_message_count != 0
                            or data.is_streaming is not False
                            or data.is_compacting is not False
                        ):
                            raise NativePiUnavailable(
                                "Selected first source runtime or inode differs"
                            )
                        startup_revision = _fresh_selected_revision(fresh_selected, started=True)
                        if startup_revision[:2] != selected_revision[:2]:
                            raise NativePiUnavailable("Selected startup changed enrolled inode")
                        selected_revision = startup_revision
                    break
                command = commands.Prompt(id="native-prompt", input_id=input_id, message=prompt)
                if prompt_send_boundary is None:
                    await send(command)
                else:
                    # A dedicated raw writer holds admission through every actual pipe
                    # write, independent of owner-loop lifecycle callbacks and drain.
                    # There is no buffered prompt remainder to flush after revocation.
                    await send_fenced_prompt(
                        stdin,
                        channel.encode(command),
                        (
                            (lambda: prompt_send_boundary(actual_file, selected_revision))
                            if fresh_selected is not None
                            else (lambda: prompt_send_boundary(actual_file))
                        ),
                        timeout=deadline - asyncio.get_running_loop().time(),
                    )
                accepted = False
                input_event: pi.InputCommitted | None = None
                contexts: list[pi.ContextCommitted] = []
                chunks: list[str] = []
                final_messages: list[str] = []
                terminal_error: str | None = None
                while True:
                    event = await next_event()
                    if isinstance(event, pi.Response) and event.id == "native-prompt":
                        if (
                            accepted
                            or event.command is not commands.Prompt
                            or event.success is not True
                        ):
                            raise NativePiUnavailable("Native Pi did not accept the tracked prompt")
                        accepted = True
                    elif isinstance(event, pi.InputCommitted) and event.input_id == input_id:
                        if input_event is not None:
                            raise NativePiUnavailable("Native Pi repeated the input commitment")
                        input_event = event
                    elif isinstance(event, pi.ContextCommitted) and event.input_id == input_id:
                        contexts.append(event)
                    elif isinstance(event, pi.MessageUpdate):
                        delta = event.assistant_message_event
                        if isinstance(delta, TextDelta):
                            text = delta.delta
                            chunks.append(text)
                    elif isinstance(event, pi.MessageEnd):
                        message = event.message
                        if message is not None and message.assistant:
                            content = message.content
                            if message.error_message:
                                terminal_error = str(message.error_message)
                                continue
                            if content is None or isinstance(content, str):
                                raise NativePiUnavailable(
                                    "Native Pi assistant content is malformed"
                                )
                            if message.stop_reason == "toolUse" and tool_socket is not None:
                                tool_socket.announce(content)
                                chunks.clear()  # Tool-round text is not the final response.
                                final_messages.clear()
                            elif message.stop_reason == "stop":
                                if tool_socket is not None:
                                    tool_socket.assert_complete()
                                parts: list[str] = []
                                for item in content:
                                    if item.final_text_allowed:
                                        parts.append(item.text)
                                    else:
                                        raise NativePiUnavailable(
                                            "Native Pi assistant returned non-text content"
                                        )
                                final_messages.append("".join(parts))
                            else:
                                terminal_error = (
                                    "Model output limit reached"
                                    if message.stop_reason == "length"
                                    else "Provider returned an unsuccessful terminal"
                                )
                    elif isinstance(event, pi.ToolExecutionStart):
                        if (
                            tool_socket is None
                            or not accepted
                            or input_event is None
                            or not contexts
                        ):
                            raise NativePiUnavailable(
                                "Native Pi tool preceded tracked context proof"
                            )
                        _verify_context(
                            actual_file, input_id, session_id, input_event, contexts[-1]
                        )
                        tool_socket.tool_started(event)
                    elif isinstance(event, pi.ToolExecutionEnd):
                        if tool_socket is None:
                            raise NativePiUnavailable("Native Pi tool has no owner policy")
                        tool_socket.tool_finished(event, input_id)
                    elif isinstance(event, pi.AgentSettled):
                        break
                    elif isinstance(event, pi.Response) and issubclass(
                        event.command, commands.MutatesSession
                    ):
                        raise NativePiUnavailable(
                            "Native Pi session identity changed during a turn"
                        )
                    if observe_event is not None:
                        await observe_event(event)
                if not accepted or input_event is None or not contexts:
                    raise NativePiUnavailable("Native Pi did not commit a tracked model context")
                if terminal_error is not None:
                    proof = _verify_context(
                        actual_file, input_id, session_id, input_event, contexts[-1]
                    )
                    raise NativePiTerminalFailure(terminal_error, proof, provider, model)
                if tool_socket is not None:
                    tool_socket.assert_complete()
                if (
                    len(final_messages) != 1
                    or not final_messages[0]
                    or "".join(chunks) != final_messages[0]
                ):
                    raise NativePiUnavailable(
                        "Native Pi has no unique authoritative completed response"
                    )
                proof = _verify_context(
                    actual_file, input_id, session_id, input_event, contexts[-1]
                )
                if selected_tool_mode is not None:
                    selected_tool_mode.finish()
                return NativeTurnResult(
                    final_messages[0].strip(),
                    proof,
                    tool_socket.selected_call_id if tool_socket is not None else None,
                )
            except SelectedToolDenied as error:
                raise NativePiUnavailable(str(error)) from error
            except PromptSendUnknown as error:
                raise NativePiUnavailable(
                    f"Native Pi prompt send is UNKNOWN; no retry: {error}"
                ) from error
            finally:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
    except (TimeoutError, OSError) as error:
        raise NativePiUnavailable("Native Pi tracked turn failed; send may be UNKNOWN") from error
    finally:
        if tool_socket is not None:
            await tool_socket.close()
