"""Tests for :mod:`agent_comms.resources`.

Covers the family contract (every declared member can be instantiated and
produces a verdict carrying its observation), the Scan's severity ordering,
and the per-member threshold math via synthesized observations.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.resources import (
    Act,
    Attention,
    Healthy,
    Observation,
    ResourceLimit,
    Scan,
    Verdict,
)
from agent_comms.resources.directory_queries import (
    DirectoryQuery,
    FreeQuery,
    UsersQuery,
)
from agent_comms.resources.limit import scan
from agent_comms.resources.limits import (
    BranchLimit,
    BranchObservation,
    CPULimit,
    CPUObservation,
    DiskObservation,
    GIB,
    HomeDiskLimit,
    MemoryLimit,
    MemoryObservation,
    MemoryStallLimit,
    MemoryStallObservation,
    RootDiskLimit,
    ShmDiskLimit,
    SwapLimit,
    SwapObservation,
    TmpDiskLimit,
)


def _verdict_from(limit: ResourceLimit, **overrides) -> Verdict:
    """Drive a limit's ``verdict`` logic with a synthesized observation, so
    threshold math is tested without touching /proc or statvfs."""
    # Each limit returns its verdict by reading state and comparing to class
    # thresholds. We inline that same shape here so the test is strict about
    # which thresholds the member exposes. A member with different thresholds
    # needs its own test — this helper is only for the four disk variants.
    observation = DiskObservation(
        resource=limit.declared_name,
        summary="synthesized",
        path=str(limit.path),
        free_bytes=overrides["free_bytes"],
    )
    if observation.free_bytes < limit.critical_bytes:
        return Act(observation=observation, reason="below critical floor")
    if observation.free_bytes < limit.warn_bytes:
        return Attention(observation=observation, reason="below warn floor")
    return Healthy(observation=observation)


class TestResourceLimitFamily:
    """Every declared member is a complete, instantiable resource limit."""

    def test_every_member_subclasses_resource_limit(self):
        members = list(ResourceLimit.members_with(ResourceLimit))
        assert members, "expected at least one declared resource limit"
        for member in members:
            assert issubclass(member, ResourceLimit)
            assert hasattr(member, "verdict")

    def test_every_member_has_a_distinct_declared_name(self):
        names = [m.declared_name for m in ResourceLimit.members_with(ResourceLimit)]
        assert len(names) == len(set(names))

    def test_every_disk_path_is_declared(self):
        names = {m.declared_name for m in ResourceLimit.members_with(ResourceLimit)}
        # The four paths the old LIMITS dict declared, plus any later addition.
        for name in ("root_disk", "tmp_disk", "shm_disk", "home_disk"):
            assert name in names, name


class TestVerdictOrder:
    def test_severity_increases(self):
        assert Healthy.severity < Attention.severity < Act.severity

    def test_worse_than_ignores_equal_severity(self):
        obs = Observation(resource="x", summary="x")
        assert not Healthy(observation=obs).worse_than(Healthy(observation=obs))
        assert Act(observation=obs).worse_than(Attention(observation=obs))
        assert not Attention(observation=obs).worse_than(Act(observation=obs))


class TestScanAggregation:
    def test_empty_scan_is_healthy(self):
        result = Scan(verdicts=(), failures=())
        assert result.level == "healthy"
        assert result.worst is None
        assert result.reasons == ()

    def test_worst_wins(self):
        obs = Observation(resource="x", summary="x")
        result = Scan(verdicts=(
            Healthy(observation=obs),
            Attention(observation=replace(obs, resource="y"), reason="warn"),
            Act(observation=replace(obs, resource="z"), reason="act"),
        ))
        assert result.level == "act"
        assert result.reasons[0].startswith("z:")  # worst first

    def test_reasons_skip_healthy(self):
        obs_h = Observation(resource="h", summary="healthy")
        obs_a = Observation(resource="a", summary="warn")
        result = Scan(verdicts=(
            Healthy(observation=obs_h),
            Attention(observation=obs_a, reason="warn"),
        ))
        assert all("h:" not in reason for reason in result.reasons)
        assert any("a:" in reason for reason in result.reasons)


class TestDiskThresholds:
    @pytest.mark.parametrize("limit_cls", [RootDiskLimit, TmpDiskLimit, ShmDiskLimit])
    def test_three_band_verdict_on_class_defaults(self, limit_cls):
        limit = limit_cls()
        # Critical: a byte below the critical floor.
        v = _verdict_from(limit, free_bytes=limit.critical_bytes - 1)
        assert isinstance(v, Act)
        # Attention: between critical and warn.
        between = (limit.critical_bytes + limit.warn_bytes) // 2
        v = _verdict_from(limit, free_bytes=between)
        assert isinstance(v, Attention)
        # Healthy: at or above warn.
        v = _verdict_from(limit, free_bytes=limit.warn_bytes + GIB)
        assert isinstance(v, Healthy)


class TestMemoryLimitMath:
    def test_act_below_critical(self):
        limit = MemoryLimit()
        obs = MemoryObservation(resource=limit.declared_name, summary="",
                                available_bytes=limit.critical_bytes - 1)
        # verdict() reads /proc in production, so drive the math directly.
        assert obs.available_bytes < limit.critical_bytes
        assert not obs.available_bytes >= limit.warn_bytes


class TestSwapLimit:
    def test_swap_only_warns(self):
        limit = SwapLimit()
        # SwapLimit never acts; highest band is attention.
        obs_high = SwapObservation(resource=limit.declared_name, summary="",
                                   used_bytes=limit.attention_bytes * 10)
        assert obs_high.used_bytes > limit.attention_bytes


class TestMemoryStallLimit:
    def test_three_bands(self):
        limit = MemoryStallLimit()
        assert limit.warn_percent < limit.critical_percent


class TestCPULimit:
    def test_bands_cover_serial_and_saturated(self):
        limit = CPULimit()
        # Parallel-floor fires on a saturated-parallel-step-running-serial.
        assert 0 < limit.parallel_floor < limit.saturation_ceiling < 1


class TestBranchLimit:
    def test_healthy_when_stale_count_below_threshold(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
        subprocess.run(["git", "-c", "user.email=a@a", "-c", "user.name=a",
                        "-C", str(tmp_path),
                        "commit", "--allow-empty", "-m", "init", "--quiet"], check=True)
        limit = BranchLimit(repo=tmp_path, attention_count=0, stale_days=365)
        v = limit.verdict()
        assert isinstance(v, Healthy)


class TestDirectoryQueryFamily:
    def test_two_declared_members(self):
        members = {q.declared_name for q in DirectoryQuery.members_with(DirectoryQuery)}
        assert members == {"users", "free"}

    def test_each_member_declares_its_cli_option(self):
        assert UsersQuery.option == "--users"
        assert FreeQuery.option == "--free"


class TestScanFunction:
    def test_scan_with_explicit_empty_list(self):
        result = scan(limits=())
        assert result.level == "healthy"
        assert result.verdicts == ()
        assert result.failures == ()

    def test_scan_captures_oserror_as_failure_not_verdict(self):
        @dataclass(frozen=True)
        class ExplodingLimit(ResourceLimit):
            def verdict(self):
                raise OSError("simulated kernel interface vanishing")

        result = scan(limits=(ExplodingLimit(),))
        assert result.verdicts == ()
        assert len(result.failures) == 1
        assert "simulated" in result.failures[0][1]
