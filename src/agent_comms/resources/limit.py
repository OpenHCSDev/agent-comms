"""``ResourceLimit`` and its verdict family.

A resource limit watches one host fact (disk free, memory headroom, CPU share,
stale git branches, …), observes it and reports a verdict. The three verdicts
are an ordered family: ``Healthy`` < ``Attention`` < ``Act``. Consumers take the
worst over every member of a scan.

The hand-written predecessor (``tools/agent_resource_check.py``) flattened both
the watch table and the escalation thresholds into one string-keyed dict plus
one ``severity()`` function. Paper1 §1 Corollary 4.10: the facts the dict did
not derive, the function restated. Here the facts live on the class.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from agent_comms.nominal.declared_family import DeclaredFamily


@dataclass(frozen=True)
class Observation:
    """Measurement a limit took of its resource.

    Members carry whatever structured reading their resource yields (free bytes,
    pressure percent, branch count, …). The base record holds the limit's
    declared_name and a human-readable summary so a verdict can report both
    without re-pulling the limit's record. Subclasses add typed fields; the
    verdict reads them through the limit that returned the observation.
    """
    resource: str
    summary: str

    def contribute_report(self, free_gib: dict[str, float], report: dict[str, object]) -> None:
        """Record any top-level metrics this observation owns in the JSON scan report."""


@dataclass(frozen=True)
class Verdict(DeclaredFamily, affix="Verdict"):
    """What a resource reading means. Ordered: ``Healthy`` < ``Attention`` < ``Act``."""
    severity: ClassVar[int] = 0
    observation: Observation

    def worse_than(self, other: Verdict) -> bool:
        return self.severity > other.severity

    @property
    def report_reason(self) -> str:
        return ""


@dataclass(frozen=True)
class HealthyVerdict(Verdict):
    """The resource is within its declared band. No action."""


@dataclass(frozen=True)
class AlertVerdict(Verdict, declared_name=None):
    """A verdict outside the healthy band carries why it fired."""
    reason: str = ""

    @property
    def report_reason(self) -> str:
        return self.reason


@dataclass(frozen=True)
class AttentionVerdict(AlertVerdict):
    """The resource crossed its warn threshold. Report, right-size, keep working."""
    severity: ClassVar[int] = 1


@dataclass(frozen=True)
class ActVerdict(AlertVerdict):
    """The resource crossed its critical threshold. The watcher's action fires."""
    severity: ClassVar[int] = 2


# Shorter names for external callers; the family members keep the ``Verdict`` affix.
Healthy = HealthyVerdict
Attention = AttentionVerdict
Act = ActVerdict


@dataclass(frozen=True)
class ResourceLimit(DeclaredFamily, affix="Limit",
                    discovery_package="agent_comms.resources"):
    """One resource the fleet watches, by declaration.

    Every member owns two facts on itself: its thresholds (declared as class
    fields or ClassVars) and how to read-and-decide on this resource (one
    ``verdict`` method). A member returns one :class:`Verdict` carrying its
    observation; the consumer (the CLI, a heartbeat, a session hook) acts on it.

    ``verdict`` may raise :class:`OSError` on a kernel interface that disappeared
    between readers (``/proc`` unmounted, a permission flip). Those are not
    verdict signals, they are host failures; the scanner surfaces them in its
    own record.
    """

    @abstractmethod
    def verdict(self) -> Verdict:
        """Observe the resource and decide what the observation means."""


@dataclass(frozen=True)
class Scan:
    """Result of one pass through every :class:`ResourceLimit` member."""
    verdicts: tuple[Verdict, ...]
    failures: tuple[tuple[str, str], ...] = ()

    @property
    def worst(self) -> Verdict | None:
        return max(self.verdicts, key=lambda verdict: verdict.severity, default=None)

    @property
    def level(self) -> str:
        worst = self.worst
        if worst is None:
            return "healthy"
        return worst.declared_name.removesuffix("_verdict")

    @property
    def reasons(self) -> tuple[str, ...]:
        """The summaries of every non-healthy verdict, ordered worst first."""
        return tuple(
            f"{verdict.observation.resource}: {verdict.observation.summary}"
            for verdict in sorted(self.verdicts, key=lambda v: -v.severity)
            if verdict.severity > Healthy.severity
        )


def scan(limits: tuple[ResourceLimit, ...] | None = None) -> Scan:
    """Run every declared resource limit and collect the verdicts.

    Passing ``limits`` runs those explicitly; otherwise every member of the
    family declared in this package runs. A member whose observation raised is
    recorded as a failure, not a verdict: the scan surfaces the host problem
    instead of pretending the resource is healthy.
    """
    if limits is None:
        limits = tuple(cls() for cls in ResourceLimit.members_with(ResourceLimit))
    verdicts: list[Verdict] = []
    failures: list[tuple[str, str]] = []
    for limit in limits:
        try:
            verdicts.append(limit.verdict())
        except OSError as error:
            failures.append((limit.declared_name, str(error)))
    return Scan(tuple(verdicts), tuple(failures))
