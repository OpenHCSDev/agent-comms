"""Queries that answer "which running processes hold handles on these paths".

The old ``DirectoryUsers`` / ``FreeDirectories`` pair in
``tools/agent_resource_check.py`` discovered its two report modes with
``__subclasses__()`` on the base class — MEMB-1, the family-via-roster
antipattern. Here the modes are declared members of a :class:`DirectoryQuery`
family; the CLI dispatches by iterating that family.

Process-stat parsing is delegated to :class:`LinuxPlatform` in
``child_process``: one owner for ``/proc/*/stat`` reading, and the
kernel-thread filter uses the new ``flags`` field added to
:class:`LinuxProcessRecord`.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from abc import abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TextIO

from agent_comms.execution.child_process import LinuxPlatform
from agent_comms.nominal.declared_family import DeclaredFamily


@dataclass
class ProcessPathUse:
    """One process observation; paths come from interface matches."""
    pid: int
    start_time: int | None = None
    matches: dict[str, list[str]] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)

    @property
    def paths(self) -> list[str]:
        return sorted({path for paths in self.matches.values() for path in paths})

    def emit(self, stream: TextIO) -> None:
        print(json.dumps({**asdict(self), "paths": self.paths}, sort_keys=True), file=stream)


class DirectoryQuery(DeclaredFamily, affix="Query",
                     discovery_package="agent_comms.resources"):
    """Report mode for a path-ownership walk.

    One member declares one way of reporting: list every process holding a
    handle, or list every path with no process holding it. Both read the same
    /proc walk; each carries its own :meth:`report` for the exit-code and
    stdout shape the CLI expects.
    """

    option: str = ""  # Overridden per member. Used by the CLI as the flag name.

    @abstractmethod
    def report(self, walk: ProcessPathWalk) -> int:
        """Emit the query's output and return the process exit code."""


class UsersQuery(DirectoryQuery):
    """List every process that holds any handle on the given paths."""
    option = "--users"

    def report(self, walk: ProcessPathWalk) -> int:
        found = False
        for process in walk:
            process.emit(sys.stdout)
            found = True
        return int(found)


class FreeQuery(DirectoryQuery):
    """List every requested path that no process holds; refuse to clear any
    path when at least one process is unreadable (we might be missing it)."""
    option = "--free"

    def report(self, walk: ProcessPathWalk) -> int:
        processes = tuple(walk)
        unreadable = tuple(process for process in processes if process.unreadable)
        if unreadable:
            for process in unreadable:
                process.emit(sys.stderr)
            return 1
        used = set().union(*(process.paths for process in processes), set())
        for path in walk.paths:
            if str(path) not in used:
                print(path)
        return 0


PF_KTHREAD = 0x00200000
_SHELL_BOUNDARIES = frozenset(
    (b";", b"&&", b"||", b"|", b"&", b"(", b")", b">", b">>", b"<", b"<<", b">&", b"<&")
)


@dataclass
class ProcessPathWalk:
    """One pass through /proc that reports which processes hold any of these
    paths across their interfaces (exe, cwd, cmdline, environ, maps, fd/*).

    The walk filters out itself (so the paths passed as CLI args don't match
    the walker's own command line), its ancestors (so a shell parent doesn't
    show up for the paths it passed in), and kernel threads (PF_KTHREAD).
    """
    paths: tuple[Path, ...]
    query_options: tuple[bytes, ...]

    def __post_init__(self) -> None:
        self.paths = tuple(dict.fromkeys(path.expanduser().absolute() for path in self.paths))
        aliases: dict[bytes, set[str]] = {}
        for path in self.paths:
            for name in (os.fsencode(path), os.fsencode(os.path.realpath(path))):
                aliases.setdefault(name.rstrip(b"/") or b"/", set()).add(str(path))
        # Prefer the longest alias; include its queried ancestors.
        self.path_matches = {
            name: set().union(*(owners for parent, owners in aliases.items()
                               if name == parent or name.startswith(parent.rstrip(b"/") + b"/")))
            for name in aliases
        }
        expressions = (re.escape(name) + (rb"(?=$|[/\x00\s:'\"])" if name != b"/" else b"")
                       for name in sorted(aliases, key=len, reverse=True))
        self.pattern = re.compile(b"(?:" + b"|".join(expressions) + b")")
        command = Path(sys.argv[0])
        self.commands = {
            os.fsencode(str(command)),
            os.fsencode(str(command.absolute())),
            os.fsencode(command.name),
        }
        self.ancestors: set[tuple[int, int]] = set()
        self.platform = LinuxPlatform()
        pid = os.getpid()
        while pid:
            try:
                record = self.platform._stat(pid)
            except (ProcessLookupError, OSError, ValueError, IndexError):
                break
            key = (record.identity.pid, record.identity.start_time)
            if key in self.ancestors:
                break
            self.ancestors.add(key)
            pid = record.parent

    def _without_query(self, arguments: list[bytes]) -> list[bytes]:
        """Strip our own ``--users``/``--free`` argument block from an argv
        copy so our paths don't match ourselves in the walk."""
        result: list[bytes] = []
        checking = False
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument in self.commands:
                checking = True
            elif checking and argument.split(b"=", 1)[0] in self.query_options:
                index += 1
                while (index < len(arguments)
                        and arguments[index] not in _SHELL_BOUNDARIES
                        and not arguments[index].startswith(b"-")):
                    index += 1
                continue
            elif argument in _SHELL_BOUNDARIES:
                checking = False
            result.append(argument)
            index += 1
        return result

    def _command_line(self, data: bytes) -> bytes:
        arguments = data.split(b"\x00")
        for index, argument in enumerate(arguments):
            if (not any(option in argument for option in self.query_options)
                    or not any(command in argument for command in self.commands)):
                continue
            try:
                lexer = shlex.shlex(os.fsdecode(argument), posix=True, punctuation_chars=True)
                lexer.whitespace_split = True
                lexer.commenters = ""
                tokens = [os.fsencode(token) for token in lexer]
            except ValueError:
                continue  # Unparsed arguments remain visible, never clear.
            arguments[index] = b" ".join(self._without_query(tokens))
        return b"\x00".join(self._without_query(arguments))

    def _still_live(self, pid: int, before_key: tuple[int, int], result: ProcessPathUse) -> bool:
        try:
            record = self.platform._stat(pid)
        except (FileNotFoundError, ProcessLookupError):
            return False
        except (PermissionError, OSError, ValueError, IndexError) as error:
            result.unreadable["stat"] = str(error)
            return True
        after_key = (record.identity.pid, record.identity.start_time)
        if after_key != before_key:
            result.unreadable["stat"] = "PID changed during inspection"
        return True

    def _read_interface(self, pid: int, before_key: tuple[int, int], name: str,
                        read, result: ProcessPathUse) -> bool:
        try:
            data = os.fsencode(read())
        except (FileNotFoundError, ProcessLookupError) as error:
            if not self._still_live(pid, before_key, result):
                return False
            if not name.startswith("fd/"):
                result.unreadable[name] = str(error)
        except PermissionError as error:
            result.unreadable[name] = str(error)
        else:
            if name == "cmdline" and before_key in self.ancestors:
                data = self._command_line(data)
            paths = set().union(*(self.path_matches[match.group()]
                                  for match in self.pattern.finditer(data)), set())
            if paths:
                result.matches[name] = sorted(paths)
        return True

    def _inspect(self, pid: int) -> ProcessPathUse | None:
        result = ProcessPathUse(pid)
        try:
            record = self.platform._stat(pid)
        except (FileNotFoundError, ProcessLookupError):
            return None
        except (PermissionError, OSError, ValueError, IndexError) as error:
            result.unreadable["stat"] = str(error)
            return result
        if record.flags & PF_KTHREAD:
            return None  # kernel thread; no userspace handles
        before_key = (record.identity.pid, record.identity.start_time)
        result.start_time = record.identity.start_time
        proc = Path(f"/proc/{pid}")
        readers = (
            ("exe", (proc / "exe").readlink),
            ("cwd", (proc / "cwd").readlink),
            ("cmdline", (proc / "cmdline").read_bytes),
            ("environ", (proc / "environ").read_bytes),
            ("maps", (proc / "maps").read_bytes),
        )
        for name, read in readers:
            if not self._read_interface(pid, before_key, name, read, result):
                return None
        try:
            for descriptor in (proc / "fd").iterdir():
                if not self._read_interface(pid, before_key, "fd/" + descriptor.name,
                                            descriptor.readlink, result):
                    return None
        except (FileNotFoundError, ProcessLookupError) as error:
            if not self._still_live(pid, before_key, result):
                return None
            result.unreadable["fd"] = str(error)
        except PermissionError as error:
            result.unreadable["fd"] = str(error)
        if not self._still_live(pid, before_key, result):
            return None
        return result if result.matches or result.unreadable else None

    def __iter__(self):
        for proc in sorted(
            Path("/proc").iterdir(),
            key=lambda entry: int(entry.name) if entry.name.isdecimal() else -1,
        ):
            if not proc.name.isdecimal() or int(proc.name) == os.getpid():
                continue
            result = self._inspect(int(proc.name))
            if result is not None:
                yield result
