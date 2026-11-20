"""Declared :class:`ResourceLimit` members.

The old table was a module dict keyed by path strings, four tuples, two inline
thresholds inside ``severity()``, plus an untyped cascade of warn/critical arms.
Here each limit is a class, its threshold band is declared on the class, its
``verdict`` reads the typed record it needs and compares two numbers. No shared
switch, no shared string table.
"""
from __future__ import annotations

import datetime
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from agent_comms.projects.storage_rules import StorageRule
from agent_comms.resources.limit import (
    Act,
    Attention,
    Healthy,
    Observation,
    ResourceLimit,
    Verdict,
)
from agent_comms.resources.procfs import CPULoad, MemoryInfo, MemoryPressure

GIB = 1024**3


@dataclass(frozen=True)
class DiskObservation(Observation):
    path: str = ""
    free_bytes: int = 0


@dataclass(frozen=True)
class DiskLimit(ResourceLimit, declared_name=None):
    """Free space on one mounted path. Not a family member itself; its concrete
    subclasses below hold the thresholds that lived inline in the old
    ``LIMITS`` dict. One dynamic subclass (``HomeDiskLimit``) reads the
    critical floor from :class:`StorageRule`.
    """
    path: ClassVar[Path] = Path("/")
    warn_bytes: ClassVar[int] = 8 * GIB
    critical_bytes: ClassVar[int] = 4 * GIB

    def verdict(self) -> Verdict:
        info = os.statvfs(str(self.path))
        free = info.f_bavail * info.f_frsize
        observation = DiskObservation(
            resource=self.declared_name,
            summary=f"{self.path} free {free / GIB:.1f} GiB",
            path=str(self.path),
            free_bytes=free,
        )
        if free < self.critical_bytes:
            return Act(observation=observation, reason="below critical floor")
        if free < self.warn_bytes:
            return Attention(observation=observation, reason="below warn floor")
        return Healthy(observation=observation)


class RootDiskLimit(DiskLimit):
    path = Path("/")
    warn_bytes = 8 * GIB
    critical_bytes = 4 * GIB


class TmpDiskLimit(DiskLimit):
    path = Path("/tmp")
    warn_bytes = 4 * GIB
    critical_bytes = 2 * GIB


class ShmDiskLimit(DiskLimit):
    path = Path("/dev/shm")
    warn_bytes = 4 * GIB
    critical_bytes = 2 * GIB


@dataclass(frozen=True)
class HomeDiskLimit(DiskLimit):
    """``/home`` free space with the critical band read from the project's
    :class:`StorageRule` instead of a literal; its warn band stays declared."""
    path: ClassVar[Path] = Path("/home")
    warn_bytes: ClassVar[int] = 20 * GIB
    root: Path = field(default_factory=lambda: Path("/var/tmp/agent-comms-live-20260927-wzjtqhza"))

    @property
    def critical_bytes(self) -> int:
        return StorageRule.load(self.root).floor_gib * GIB


@dataclass(frozen=True)
class MemoryObservation(Observation):
    available_bytes: int = 0


class MemoryLimit(ResourceLimit):
    """Available RAM. The old script spelled the numbers (3 GiB critical,
    8 GiB warn) inline inside a six-line if-cascade across every memory signal."""
    warn_bytes: ClassVar[int] = 8 * GIB
    critical_bytes: ClassVar[int] = 3 * GIB

    def verdict(self) -> Verdict:
        info = MemoryInfo.read()
        observation = MemoryObservation(
            resource=self.declared_name,
            summary=f"RAM available {info.available_bytes / GIB:.1f} GiB",
            available_bytes=info.available_bytes,
        )
        if info.available_bytes < self.critical_bytes:
            return Act(observation=observation, reason="below critical floor")
        if info.available_bytes < self.warn_bytes:
            return Attention(observation=observation, reason="below warn floor")
        return Healthy(observation=observation)


@dataclass(frozen=True)
class SwapObservation(Observation):
    used_bytes: int = 0


class SwapLimit(ResourceLimit):
    """Swap used above this attention threshold. Idle pages in swap are not
    pressure by themselves — that's :class:`MemoryStallLimit`'s job — but a
    sustained high swap-used combined with any stall is the right warn signal."""
    attention_bytes: ClassVar[int] = 8 * GIB

    def verdict(self) -> Verdict:
        info = MemoryInfo.read()
        observation = SwapObservation(
            resource=self.declared_name,
            summary=f"swap used {info.swap_used_bytes / GIB:.1f} GiB",
            used_bytes=info.swap_used_bytes,
        )
        if info.swap_used_bytes > self.attention_bytes:
            return Attention(observation=observation, reason="swap used over threshold")
        return Healthy(observation=observation)


@dataclass(frozen=True)
class MemoryStallObservation(Observation):
    full_avg60_percent: float = 0.0


class MemoryStallLimit(ResourceLimit):
    """Kernel PSI full memory stall over 60 s. The real backlog signal: idle
    pages parked in swap don't count, actual stalled wall-time does."""
    warn_percent: ClassVar[float] = 2.0
    critical_percent: ClassVar[float] = 10.0

    def verdict(self) -> Verdict:
        pressure = MemoryPressure.read()
        observation = MemoryStallObservation(
            resource=self.declared_name,
            summary=f"memory stall {pressure.full_avg60_percent:.1f}% over 60 s",
            full_avg60_percent=pressure.full_avg60_percent,
        )
        if pressure.full_avg60_percent > self.critical_percent:
            return Act(observation=observation, reason="PSI full avg60 above critical")
        if pressure.full_avg60_percent > self.warn_percent:
            return Attention(observation=observation, reason="PSI full avg60 above warn")
        return Healthy(observation=observation)


@dataclass(frozen=True)
class CPUObservation(Observation):
    load_1min: float = 0.0
    cores: int = 0
    utilization: float = 0.0


class CPULimit(ResourceLimit):
    """Host 1-minute load vs online cores.

    Reports :class:`Attention` below ``parallel_floor`` (a step that should be
    saturating parallel work is instead running serial on one core — the
    Nov 8, 2026 install at 14% CPU for 40 minutes that nobody noticed), and
    :class:`Act` above ``saturation_ceiling`` (the host is so loaded that a
    background job is stealing from active work). The fact
    :mod:`rules/evidence.md` carries as a human rule ("look at the machine
    while it runs") turned into a running check.
    """
    parallel_floor: ClassVar[float] = 0.25
    saturation_ceiling: ClassVar[float] = 0.95

    def verdict(self) -> Verdict:
        load = CPULoad.read()
        observation = CPUObservation(
            resource=self.declared_name,
            summary=f"load {load.load_1min:.2f} on {load.cores} cores "
                    f"({load.utilization * 100:.0f}%)",
            load_1min=load.load_1min,
            cores=load.cores,
            utilization=load.utilization,
        )
        if load.utilization > self.saturation_ceiling:
            return Act(observation=observation, reason="host saturated")
        if load.utilization < self.parallel_floor:
            return Attention(
                observation=observation,
                reason="low utilization — serial step on parallel host?",
            )
        return Healthy(observation=observation)


@dataclass(frozen=True)
class BranchObservation(Observation):
    local_branches: int = 0
    stale_branches: tuple[str, ...] = ()


@dataclass(frozen=True)
class BranchLimit(ResourceLimit):
    """Stale local branches in a git checkout.

    Attention if more than ``attention_count`` stale local branches exist
    (longer than ``stale_days`` since their last commit, not currently checked
    out). Default repo is the current working directory; the CLI reads itself
    without args.
    """
    repo: Path = field(default_factory=Path.cwd)
    stale_days: int = 30
    attention_count: int = 10

    def verdict(self) -> Verdict:
        out = subprocess.run(
            ["git", "-C", str(self.repo), "for-each-ref",
             "--format=%(refname:short)\t%(committerdate:iso8601-strict)",
             "refs/heads/"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if out.returncode != 0:
            raise OSError(f"git for-each-ref refused: {out.stderr.strip()}")
        current = subprocess.run(
            ["git", "-C", str(self.repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=self.stale_days)
        stale: list[str] = []
        total = 0
        for line in out.stdout.splitlines():
            if not line:
                continue
            total += 1
            name, _, when = line.partition("\t")
            if not when or name == current:
                continue
            try:
                parsed = datetime.datetime.fromisoformat(when)
            except ValueError:
                continue
            if parsed < cutoff:
                stale.append(name)
        observation = BranchObservation(
            resource=self.declared_name,
            summary=f"{total} local branches, {len(stale)} stale over {self.stale_days}d",
            local_branches=total,
            stale_branches=tuple(sorted(stale)),
        )
        if len(stale) > self.attention_count:
            return Attention(
                observation=observation,
                reason=f"{len(stale)} stale branches: consolidate",
            )
        return Healthy(observation=observation)
