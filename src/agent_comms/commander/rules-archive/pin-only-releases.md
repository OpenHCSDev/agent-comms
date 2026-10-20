---
name: pin-only-releases
description: "Owner rule: pin versions only at PyPI releases; dev stacks and our own sibling packages always track latest"
type: feedback
---

Pinning is limited to PyPI releases. Inside a development stack, our own packages (Toad, Textual fork, objectstate, formbridge, ui-theme, etc. from OpenHCSDev) always track the latest `main`; upgrades relock to latest (`uv lock --upgrade`) instead of trusting committed commit pins. Owner, 2026-10-18: "we should just use latest", "why would we not always use latest?", "shouldn't pinning be limited to releases on pypi?"

**Why:** the agent-comms stack lock pinned Toad at an old commit after Core renamed a module (P312) and Toad main followed; toad-comms broke on main. A commit pin restates what the branch owns and goes stale when either side moves.

**How to apply:** replace committed dev pins with "latest + an import/compat check" in the upgrade path and CI. Exception (my judgement, stated to the owner): byte-level patch targets like stock Pi in `stack/pi-native.sha256`, where patches apply with zero fuzz to a specific upstream release; moving those is a deliberate re-pin step. Related: [[merge-on-local-gates]] (full matrix gates PyPI releases).
