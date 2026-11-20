"""Host resources the fleet watches as declared facts, one family of limits.

Replaces the hand-written ``tools/agent_resource_check.py``, whose module-level
``LIMITS`` dict was keyed by path strings and whose ``severity`` function spelled
thresholds inline across five scattered ``if`` arms (IMPL-11 transition table beside
its states; IMPL-14 validation as one anonymous boolean). The paper1 move: each
resource is one declaration owning its own observation, threshold, and verdict.

Consumers iterate ``ResourceLimit.members()``; a new resource is one subclass.
"""
from __future__ import annotations

from agent_comms.resources.limit import (
    Attention,
    Act,
    Healthy,
    Observation,
    ResourceLimit,
    Scan,
    Verdict,
    scan,
)
from agent_comms.resources.procfs import MemoryInfo, MemoryPressure

__all__ = (
    "Act",
    "Attention",
    "Healthy",
    "MemoryInfo",
    "MemoryPressure",
    "Observation",
    "ResourceLimit",
    "Scan",
    "Verdict",
    "scan",
)
