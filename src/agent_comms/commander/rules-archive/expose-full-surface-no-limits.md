---
name: expose-full-surface-no-limits
description: When OpenHCS supports an external tool, expose its whole surface, not a subset; do not scope down for budget reasons
type: feedback
---

When OpenHCS integrates an external algorithm or library, expose everything it offers (every capability and parameter reachable in the tool itself or in any other platform's integration of it), not a minimal subset.

**Why:** owner, 2026-10-12, when I noted that a mature CellProfiler plugin exposes more Cellpose options than our 151-line example: "there's no reason why we aren't exposing the same amount of functions. remember there's no real limits." There is no token or effort budget reason to scope down; an integration that exposes less than competitors undercuts the claim that adding tools here is trivial.

**How to apply:** build a capability matrix against the tool's own API and the other platforms' integrations, and close every gap or state the precise reason. Prefer deriving the surface from the tool's own signatures (as the scikit-image/CuPy/pyclesperanto registries do) over hand-transcribing, so it stays complete when the tool changes. My interpretation: "no real limits" is also a reminder not to ration scope, agents or thoroughness in general; machine limits (15 GB RAM, disk) still apply. Related: [[structural-fixes-over-pushing-through]], [[full-autonomy-openhcs]].
