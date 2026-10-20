---
name: merge-on-local-gates
description: "merge on local gates (targeted tests, both ratchets rc=0, each its own command, separate from the merge); the full CI matrix runs only before a PyPI release"
type: feedback
---

**Why:** because main moves faster than the matrix and a merge sharing a command with a ratchet once passed a failing ratchet
