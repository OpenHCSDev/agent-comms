#!/home/ts/.local/share/agent-comms/current/bin/python
"""Check desktop headroom before agent work and notify on sustained pressure."""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TextIO

from agent_comms.storage_rules import StorageRule

GIB = 1024**3
STATE = Path.home() / ".local/state/agent-comms/resource-alert.json"
LIMITS = {
    "/": (8, 4),
    "/tmp": (4, 2),
    "/dev/shm": (4, 2),
}


def limits_for(root):
    return {**LIMITS, "/home": (20, StorageRule.load(root).floor_gib)}


def headroom(limits):
    free = {}
    for path in limits:
        info = os.statvfs(path)
        free[path] = info.f_bavail * info.f_frsize
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, value = line.split(":", 1)
        if name in {"MemAvailable", "SwapTotal", "SwapFree"}:
            memory[name] = int(value.strip().split()[0]) * 1024
    full = Path("/proc/pressure/memory").read_text().splitlines()[1].split()
    memory["full_avg60"] = float(dict(field.split("=") for field in full[1:])["avg60"])
    return free, memory


def severity(free, memory, limits):
    warnings = []
    critical = []
    for path, (warn_gib, critical_gib) in limits.items():
        if free[path] < critical_gib * GIB:
            critical.append(f"{path} free {free[path] / GIB:.1f} GiB")
        elif free[path] < warn_gib * GIB:
            warnings.append(f"{path} free {free[path] / GIB:.1f} GiB")
    available = memory["MemAvailable"]
    swap_used = memory["SwapTotal"] - memory["SwapFree"]
    if available < 3 * GIB:
        critical.append(f"RAM available {available / GIB:.1f} GiB")
    elif available < 8 * GIB:
        warnings.append(f"RAM available {available / GIB:.1f} GiB")
    # Idle pages parked in swap are not pressure; the kernel's stall time is.
    stalled = memory["full_avg60"]
    if stalled > 10:
        critical.append(f"memory stall {stalled:.1f}% over 60 s")
    elif stalled > 2 or swap_used > 8 * GIB:
        warnings.append(f"memory stall {stalled:.1f}% over 60 s, swap used {swap_used / GIB:.1f} GiB")
    return ("critical" if critical else "warning" if warnings else "healthy", critical + warnings)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: int
    state: bytes
    flags: int
    parent: int

    @classmethod
    def read(cls, proc):
        # stat's comm may itself contain parentheses or whitespace.
        fields = (proc / "stat").read_bytes().rsplit(b")", 1)[1].split()
        return cls(int(proc.name), int(fields[19]), fields[0], int(fields[6]), int(fields[1]))

    @property
    def key(self):
        return self.pid, self.start_time

    @property
    def userspace(self):
        # PF_KTHREAD has no userspace executable, environment or mappings.
        return self.state not in (b"Z", b"X") and not self.flags & 0x00200000


@dataclass
class DirectoryUse:
    """One process observation; paths are derived from its interface matches."""

    pid: int
    start_time: int | None = None
    matches: dict[str, list[str]] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)

    @property
    def paths(self) -> list[str]:
        return sorted({path for paths in self.matches.values() for path in paths})

    def emit(self, stream: TextIO) -> None:
        print(json.dumps({**asdict(self), "paths": self.paths}, sort_keys=True), file=stream)


class DirectoryUsers:
    """Inspect all requested paths in one /proc pass, excluding this checker."""

    option = "--users"
    shell_boundaries = frozenset((b";", b"&&", b"||", b"|", b"&", b"(", b")", b">", b">>", b"<", b"<<", b">&", b"<&"))

    @staticmethod
    def kinds() -> tuple[type["DirectoryUsers"], ...]:
        return (DirectoryUsers, *DirectoryUsers.__subclasses__())

    def __init__(self, paths):
        self.paths = tuple(dict.fromkeys(path.expanduser().absolute() for path in paths))
        aliases = {}
        for path in self.paths:
            for name in (os.fsencode(path), os.fsencode(os.path.realpath(path))):
                aliases.setdefault(name.rstrip(b"/") or b"/", set()).add(str(path))
        # Prefer the longest name, then include its queried ancestors too.
        # One path may be both a symlink alias and a child of another query.
        self.path_matches = {
            name: set().union(*(owners for parent, owners in aliases.items()
                               if name == parent or name.startswith(parent.rstrip(b"/") + b"/")))
            for name in aliases
        }
        expressions = (re.escape(name) + (rb"(?=$|[/\x00\s:'\"])" if name != b"/" else b"")
                       for name in sorted(aliases, key=len, reverse=True))
        self.pattern = re.compile(b"(?:" + b"|".join(expressions) + b")")
        self.query_options = tuple(kind.option.encode() for kind in self.kinds())
        command = Path(sys.argv[0])
        self.commands = {os.fsencode(str(command)), os.fsencode(str(command.absolute())), os.fsencode(command.name)}
        self.ancestors = set()
        pid = os.getpid()
        while pid:
            try:
                identity = ProcessIdentity.read(Path("/proc") / str(pid))
            except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
                break
            if identity.key in self.ancestors:
                break
            self.ancestors.add(identity.key)
            pid = identity.parent

    def without_query(self, arguments):
        result = []
        checking = False
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument in self.commands:
                checking = True
            elif checking and argument.split(b"=", 1)[0] in self.query_options:
                index += 1
                while (index < len(arguments) and arguments[index] not in self.shell_boundaries
                       and not arguments[index].startswith(b"-")):
                    index += 1
                continue
            elif argument in self.shell_boundaries:
                checking = False
            result.append(argument)
            index += 1
        return result

    def command_line(self, data):
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
            arguments[index] = b" ".join(self.without_query(tokens))
        return b"\x00".join(self.without_query(arguments))

    def still_userspace(self, proc: Path, before: ProcessIdentity, result: DirectoryUse) -> bool:
        """Discard a departed process, never an unreadable live one."""
        try:
            after = ProcessIdentity.read(proc)
        except (FileNotFoundError, ProcessLookupError):
            return False
        except (PermissionError, ValueError, IndexError) as error:
            result.unreadable["stat"] = str(error)
            return True
        if not after.userspace:
            return False
        if after.key != before.key:
            result.unreadable["stat"] = "PID changed during inspection"
        return True

    def read_interface(self, proc: Path, before: ProcessIdentity, name: str,
                       read: Callable[[], bytes | Path], result: DirectoryUse) -> bool:
        try:
            data = os.fsencode(read())
        except (FileNotFoundError, ProcessLookupError) as error:
            if not self.still_userspace(proc, before, result):
                return False
            if not name.startswith("fd/"):
                result.unreadable[name] = str(error)
        except PermissionError as error:
            result.unreadable[name] = str(error)
        else:
            if name == "cmdline" and before.key in self.ancestors:
                data = self.command_line(data)
            paths = set().union(*(self.path_matches[match.group()] for match in self.pattern.finditer(data)))
            if paths:
                result.matches[name] = sorted(paths)
        return True

    def inspect(self, proc):
        result = DirectoryUse(int(proc.name))
        try:
            before = ProcessIdentity.read(proc)
        except (FileNotFoundError, ProcessLookupError):
            return None
        except (PermissionError, ValueError, IndexError) as error:
            result.unreadable["stat"] = str(error)
            return result
        if not before.userspace:
            return None
        result.start_time = before.start_time
        readers = (
            ("exe", (proc / "exe").readlink),
            ("cwd", (proc / "cwd").readlink),
            ("cmdline", (proc / "cmdline").read_bytes),
            ("environ", (proc / "environ").read_bytes),
            ("maps", (proc / "maps").read_bytes),
        )
        for name, read in readers:
            if not self.read_interface(proc, before, name, read, result):
                return None
        try:
            for descriptor in (proc / "fd").iterdir():
                if not self.read_interface(proc, before, "fd/" + descriptor.name, descriptor.readlink, result):
                    return None
        except (FileNotFoundError, ProcessLookupError) as error:
            if not self.still_userspace(proc, before, result):
                return None
            result.unreadable["fd"] = str(error)
        except PermissionError as error:
            result.unreadable["fd"] = str(error)
        if not self.still_userspace(proc, before, result):
            return None
        return result if result.matches or result.unreadable else None

    def __iter__(self):
        for proc in sorted(Path("/proc").iterdir(), key=lambda entry: int(entry.name) if entry.name.isdecimal() else -1):
            if not proc.name.isdecimal() or int(proc.name) == os.getpid():
                continue
            result = self.inspect(proc)
            if result is not None:
                yield result

    def report(self) -> int:
        found = False
        for process in self:
            process.emit(sys.stdout)
            found = True
        return int(found)


class FreeDirectories(DirectoryUsers):
    """Print paths with no users; refuse to clear any path when a process is unreadable."""

    option = "--free"

    def report(self) -> int:
        processes = tuple(self)
        unreadable = tuple(process for process in processes if process.unreadable)
        if unreadable:
            for process in unreadable:
                process.emit(sys.stderr)
            return 1
        used = set().union(*(process.paths for process in processes))
        for path in self.paths:
            if str(path) not in used:
                print(path)
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'))
    parser.add_argument("--assert-headroom", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    queries = parser.add_mutually_exclusive_group()
    for kind in DirectoryUsers.kinds():
        queries.add_argument(kind.option, dest=kind.__name__, type=Path, nargs="+", metavar="PATH",
                             help=kind.__doc__)
    args = parser.parse_args()
    for kind in DirectoryUsers.kinds():
        if (paths := getattr(args, kind.__name__)) is not None:
            # Keep the caller's permissions. Unreadable processes remain explicit
            # in the report and prevent FreeDirectories from declaring a path free.
            raise SystemExit(kind(paths).report())
    limits = limits_for(args.root)
    free, memory = headroom(limits)
    level, reasons = severity(free, memory, limits)
    print(json.dumps({
        "level": level,
        "reasons": reasons,
        "free_gib": {path: round(value / GIB, 1) for path, value in free.items()},
        "ram_available_gib": round(memory["MemAvailable"] / GIB, 1),
        "swap_used_gib": round((memory["SwapTotal"] - memory["SwapFree"]) / GIB, 1),
    }, sort_keys=True))
    if args.monitor:
        try:
            previous = json.loads(STATE.read_text()).get("level")
        except (FileNotFoundError, PermissionError, ValueError):
            previous = None
        if previous != level:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({"level": level, "reasons": reasons}) + "\n")
            if level != "healthy" and shutil.which("notify-send"):
                subprocess.run(
                    ["notify-send", "-u", "critical" if level == "critical" else "normal",
                     "Agent resource pressure", "; ".join(reasons)],
                    timeout=3, check=False,
                )
    if args.assert_headroom and level != "healthy":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
