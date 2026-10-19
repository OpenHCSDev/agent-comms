"""A process's explicit launch record; never an inherited environment protocol."""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, ClassVar

from pydantic import JsonValue

from .errors import RelationViolationError
from .field_codec import FieldCodec, PathText
from .message_reference import MessageReference


class CallingThreadUnavailable(RelationViolationError):
    """This process was not explicitly launched for an owner thread."""


@dataclass(frozen=True)
class RestartEnvironment:
    """The native resource directories retained across an owner restart."""
    default_agent_directory: ClassVar[Path] = Path("~/.pi/agent")
    native_config: Annotated[Path, PathText] = default_agent_directory
    agent_directory: Annotated[Path, PathText] = default_agent_directory

    def __post_init__(self) -> None:
        object.__setattr__(self, "native_config", self.native_config.expanduser().resolve())
        object.__setattr__(self, "agent_directory", self.agent_directory.expanduser().resolve())

    def for_agent(self, directory: Path) -> RestartEnvironment:
        return replace(self, agent_directory=directory)

    def encode_native(self) -> dict[str, str]:
        """Pi, not Core, consumes this external program's configuration variable."""
        return {"PI_CODING_AGENT_DIR": str(self.agent_directory)}

    def auth_revision(self) -> tuple[int, int]:
        try:
            info = (self.native_config / "auth.json").stat()
        except FileNotFoundError:
            return 0, 0
        return info.st_mtime_ns, info.st_size


@dataclass(frozen=True)
class ThreadLaunch:
    name: str
    worktree: str
    tags: frozenset[str] = frozenset()
    parent: str | None = None
    task: str | None = None
    managed: bool = False
    startup_input: str | None = None

    @classmethod
    def current(cls) -> ThreadLaunch:
        return LaunchRuntime.running().require_thread()

    @classmethod
    def called_by(cls, name: str, canonicalize: Callable[[str], str]) -> bool:
        return canonicalize(cls.current().name) == name

    @classmethod
    def take_startup_input(cls) -> str | None:
        runtime = LaunchRuntime.running()
        thread = runtime.require_thread()
        LaunchRuntime.install(replace(runtime, thread=replace(thread, startup_input=None)))
        return thread.startup_input


@dataclass(frozen=True)
class LaunchRuntime:
    """One decoded launch for this process and explicitly selected children."""
    interpreter: Annotated[Path, PathText]
    root: Annotated[Path, PathText] | None = None
    thread: ThreadLaunch | None = None
    configuration: RestartEnvironment = field(default_factory=RestartEnvironment)
    agent_bin: str = "pi"
    agent_args: tuple[str, ...] | None = None
    agent_models: tuple[str, ...] = ()
    debug_log: Annotated[Path, PathText] | None = None
    native_origin: str | None = None
    login_provider: str | None = None
    project_socket: Annotated[Path, PathText] | None = None
    project_request: dict[str, JsonValue] | None = None
    parent_lifeline_fd: int | None = None
    annotation_grant: MessageReference | None = None

    argument: ClassVar[str] = "--comms-launch"
    _running: ClassVar[LaunchRuntime | None] = None

    @classmethod
    def running(cls) -> LaunchRuntime:
        if cls._running is None:
            raise CallingThreadUnavailable('No launch record was installed at startup')
        return cls._running

    @classmethod
    def install(cls, runtime: LaunchRuntime) -> None:
        cls._running = runtime

    @classmethod
    def read(cls, path: Path) -> LaunchRuntime:
        return FieldCodec.decode(cls, json.loads(path.read_text()))

    @classmethod
    def startup(cls, argv: list[str] | None = None) -> LaunchRuntime:
        """Consume the selected startup channel once; subprocess argv is independent."""
        args = sys.argv if argv is None else argv
        if cls.argument in args:
            index = args.index(cls.argument)
            if index + 1 >= len(args):
                raise ValueError("Launch record path is missing")
            runtime = cls.read(Path(args[index + 1]))
            del args[index:index + 2]
            cls.install(runtime)
        return cls.running()

    @classmethod
    def for_process(cls, pid: int) -> LaunchRuntime:
        args = [os.fsdecode(item) for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if item]
        try:
            index = args.index(cls.argument)
            path = args[index + 1]
        except (ValueError, IndexError) as error:
            raise CallingThreadUnavailable("Selected process has no explicit launch record") from error
        return cls.read(Path(path))

    def write(self, path: Path) -> tuple[str, str]:
        from .store_files import _atomic_write_text
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write_text(path, json.dumps(FieldCodec.encode(self), separators=(",", ":")))
        return self.argument, str(path)

    def require_root(self) -> Path:
        if self.root is None:
            raise CallingThreadUnavailable('Launch record has no selected root')
        return self.root

    def require_thread(self) -> ThreadLaunch:
        if self.thread is None:
            raise CallingThreadUnavailable("An explicit owner launch record is required")
        return self.thread

    @property
    def prefix(self) -> Path:
        return self.interpreter.parent.parent.resolve()

    @property
    def native_entrypoint(self) -> str:
        return str(self.interpreter.with_name("pi-comms-native"))

    @staticmethod
    def external_environment(source: Mapping[str, str]) -> dict[str, str]:
        # Discard obsolete inherited metadata during rolling updates; never interpret it.
        return {key: value for key, value in source.items() if not key.startswith("AGENT_COMMS_")}

    def environment(self, source: Mapping[str, str]) -> dict[str, str]:
        result = self.external_environment(source)
        result.pop("PYTHONPATH", None)
        result["VIRTUAL_ENV"] = str(self.prefix)
        return result

    def accepts_launch_record(self, argv: Sequence[str]) -> bool:
        """Identify Core entry points from their installed declarations, not a command list."""
        from importlib.metadata import distribution
        import shutil
        if not argv:
            raise ValueError('Child command is empty')
        declarations = distribution('agent-comms').entry_points
        executable = Path(shutil.which(argv[0]) or argv[0]).absolute()
        for declaration in declarations:
            if declaration.group != 'console_scripts':
                continue
            if executable == self.interpreter.with_name(declaration.name).absolute():
                return True
            if (executable == self.interpreter.absolute()
                    and tuple(argv[1:3]) == ('-m', declaration.module)):
                return True
        return False

    def require_available(self) -> None:
        if not self.interpreter.is_file() or not os.access(self.interpreter, os.X_OK):
            raise ValueError("Target interpreter is unavailable")


class RestoredRuntime(LaunchRuntime):
    """Original-process recovery preserves external import configuration."""
    def environment(self, source: Mapping[str, str]) -> dict[str, str]:
        return self.external_environment(source)
