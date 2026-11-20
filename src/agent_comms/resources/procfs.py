"""Typed records for the ``/proc`` interfaces this package reads.

The old ``tools/agent_resource_check.py`` parsed ``/proc/meminfo`` inline (key/value
split on each line, four magic field names, kilobytes-to-bytes scale) and
``/proc/pressure/memory`` inline again (second line, ``avg60`` field). Each parse
was spelled at its one use site; no record declared the kernel's shape, so a
verdict function had to reach into a ``dict[str, int]`` and remember which keys
had which units. Paper1 §2 / BOUND-1: one declaration, decoded at the boundary.

Process-stat reading stays in :mod:`agent_comms.execution.child_process` where
``LinuxPlatform._stat`` already owns it; see the limits module for the record
shape that uses ``LinuxProcessRecord``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MemoryInfo:
    """A snapshot of ``/proc/meminfo``, in bytes."""
    available_bytes: int
    swap_total_bytes: int
    swap_free_bytes: int

    @property
    def swap_used_bytes(self) -> int:
        return self.swap_total_bytes - self.swap_free_bytes

    @classmethod
    def read(cls, meminfo: Path = Path("/proc/meminfo")) -> MemoryInfo:
        wanted = ("MemAvailable", "SwapTotal", "SwapFree")
        found: dict[str, int] = {}
        for line in meminfo.read_text().splitlines():
            name, _, value = line.partition(":")
            if name in wanted:
                # /proc/meminfo reports kilobytes with "kB" suffix.
                found[name] = int(value.strip().split()[0]) * 1024
                if len(found) == len(wanted):
                    break
        missing = set(wanted) - set(found)
        if missing:
            raise OSError(f"/proc/meminfo missing fields: {sorted(missing)}")
        return cls(
            available_bytes=found["MemAvailable"],
            swap_total_bytes=found["SwapTotal"],
            swap_free_bytes=found["SwapFree"],
        )


@dataclass(frozen=True)
class MemoryPressure:
    """A snapshot of ``/proc/pressure/memory`` full-stall average.

    PSI reports idle pages parked in swap as ``some`` pressure, which is noise:
    the kernel's ``full`` line reports the fraction of wall time every task
    was stalled on memory, which is the real backlog signal.
    """
    full_avg60_percent: float

    @classmethod
    def read(cls, pressure: Path = Path("/proc/pressure/memory")) -> MemoryPressure:
        lines = pressure.read_text().splitlines()
        if len(lines) < 2:
            raise OSError("/proc/pressure/memory reported no full line")
        # Line shape: "full avg10=X avg60=Y avg300=Z total=N"
        pairs = dict(pair.split("=") for pair in lines[1].split()[1:])
        if "avg60" not in pairs:
            raise OSError("/proc/pressure/memory full line has no avg60 field")
        return cls(full_avg60_percent=float(pairs["avg60"]))


@dataclass(frozen=True)
class CPULoad:
    """A snapshot of ``/proc/loadavg``'s 1-minute load average and the host's
    online core count. A step running at ``load_1min < cores / 4`` through what
    should be a parallel phase is the serial-step smell (Nov 8, 2026)."""
    load_1min: float
    cores: int

    @classmethod
    def read(
        cls,
        loadavg: Path = Path("/proc/loadavg"),
        cores: int | None = None,
    ) -> CPULoad:
        import os
        fields = loadavg.read_text().split()
        if not fields:
            raise OSError("/proc/loadavg returned no fields")
        online = cores if cores is not None else os.cpu_count() or 1
        return cls(load_1min=float(fields[0]), cores=online)

    @property
    def utilization(self) -> float:
        return self.load_1min / self.cores if self.cores else 0.0
