---
name: constant-cost-rule
description: "nothing on a hot path scales with history size, thread count or message rate: read newest-first, decode only the viewport"
type: feedback
---

**Why:** it shuold decode from latest to oldest or jsut decode what it needs so that tis always fast and doesn't get longer with theard size
