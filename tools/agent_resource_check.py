#!/usr/bin/env python3
"""Check desktop headroom before agent work and notify on sustained pressure."""

import argparse
from dataclasses import dataclass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from agent_comms.storage_rules import StorageRule

GIB = 1024**3
STATE = Path.home() / ".local/state/agent-comms/resource-alert.json"
LIMITS = {
    "/": (8, 4),
    "/home": (20, StorageRule().floor_gib),
    "/tmp": (4, 2),
    "/dev/shm": (4, 2),
}


def headroom():
    free = {}
    for path in LIMITS:
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


def severity(free, memory):
    warnings = []
    critical = []
    for path, (warn_gib, critical_gib) in LIMITS.items():
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


class DirectoryUsers:
    """Read-only, point-in-time users of a directory, excluding this checker.

    The checker exits before removal; its own --users argument is not a user.
    All other live processes are checked, including ones we cannot inspect.
    """

    def __init__(self, path):
        path = path.expanduser().absolute()
        names = {os.fsencode(str(path)), os.fsencode(os.path.realpath(path))}
        expressions = []
        for name in names:
            name = name.rstrip(b"/") or b"/"
            expressions.append(re.escape(name) + (rb"(?=$|[/\x00\s:'\"])" if name != b"/" else b""))
        self.pattern = re.compile(b"(?:" + b"|".join(expressions) + b")")
        command = Path(sys.argv[0])
        self.commands = {os.fsencode(str(command)), os.fsencode(str(command.absolute())), os.fsencode(command.name)}
        self.ancestors = set()
        pid = os.getpid()
        while pid:
            try:
                identity = ProcessIdentity.read(Path("/proc") / str(pid))
            except (OSError, ValueError, IndexError):
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
            elif checking and argument == b"--users" and index + 1 < len(arguments):
                index += 2
                continue
            elif checking and argument.startswith(b"--users="):
                index += 1
                continue
            elif argument in (b";", b"&&", b"||", b"|", b"&", b"(", b")"):
                checking = False
            result.append(argument)
            index += 1
        return result

    def command_line(self, data):
        arguments = data.split(b"\x00")
        for index, argument in enumerate(arguments):
            if b"--users" not in argument or not any(command in argument for command in self.commands):
                continue
            # Shell -c bodies put the query in one argument. Remove only that
            # query's option/value, not other paths in the invoking command.
            try:
                lexer = shlex.shlex(os.fsdecode(argument), posix=True, punctuation_chars=True)
                lexer.whitespace_split = True
                lexer.commenters = ""
                tokens = [os.fsencode(token) for token in lexer]
            except ValueError:
                continue  # An unparsed argument remains visible, never clear.
            arguments[index] = b" ".join(self.without_query(tokens))
        return b"\x00".join(self.without_query(arguments))

    def inspect(self, proc):
        result = {"pid": int(proc.name), "matches": [], "unreadable": {}}
        try:
            before = ProcessIdentity.read(proc)
        except (FileNotFoundError, ProcessLookupError):
            return None
        except (OSError, ValueError, IndexError) as error:
            result["unreadable"]["stat"] = str(error)
            return result
        if not before.userspace:
            return None
        result["start_time"] = before.start_time
        # These are Linux's external file interfaces, not separate scan policies.
        readers = (
            ("exe", (proc / "exe").readlink),
            ("cwd", (proc / "cwd").readlink),
            ("cmdline", (proc / "cmdline").read_bytes),
            ("environ", (proc / "environ").read_bytes),
            ("maps", (proc / "maps").read_bytes),
        )
        for name, read in readers:
            try:
                data = os.fsencode(read())
                if name == "cmdline" and before.key in self.ancestors:
                    data = self.command_line(data)
                if self.pattern.search(data):
                    result["matches"].append(name)
            except OSError as error:
                result["unreadable"][name] = str(error)
        try:
            for descriptor in (proc / "fd").iterdir():
                try:
                    if self.pattern.search(os.fsencode(descriptor.readlink())):
                        result["matches"].append("fd/" + descriptor.name)
                except (FileNotFoundError, ProcessLookupError):
                    pass  # An individually closed descriptor is not an inspection failure.
                except OSError as error:
                    result["unreadable"]["fd/" + descriptor.name] = str(error)
        except OSError as error:
            result["unreadable"]["fd"] = str(error)
        try:
            after = ProcessIdentity.read(proc)
        except (FileNotFoundError, ProcessLookupError):
            return None
        except (OSError, ValueError, IndexError) as error:
            result["unreadable"]["stat"] = str(error)
        else:
            if after.key != before.key:
                result["unreadable"]["stat"] = "PID changed during inspection"
            elif not after.userspace:
                return None
        return result if result["matches"] or result["unreadable"] else None

    def __iter__(self):
        for proc in sorted(Path("/proc").iterdir(), key=lambda entry: int(entry.name) if entry.name.isdecimal() else -1):
            if not proc.name.isdecimal() or int(proc.name) == os.getpid():
                continue
            result = self.inspect(proc)
            if result is not None:
                yield result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assert-headroom", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--users", type=Path, metavar="PATH", help="list directory users and unreadable processes (excluding this checker); exit 1 if any")
    args = parser.parse_args()
    if args.users is not None:
        # Other users' /proc entries are readable only by root; read them all instead of failing on them.
        if os.geteuid() != 0:
            os.execvp("sudo", ["sudo", "-n", sys.executable, os.path.abspath(__file__), *sys.argv[1:]])
        found = False
        for process in DirectoryUsers(args.users):
            print(json.dumps(process, sort_keys=True))
            found = True
        raise SystemExit(int(found))
    free, memory = headroom()
    level, reasons = severity(free, memory)
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
        except (OSError, ValueError):
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
