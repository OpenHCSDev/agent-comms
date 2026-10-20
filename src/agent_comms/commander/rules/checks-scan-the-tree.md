---
name: checks-scan-the-tree
description: "an order check counts over the whole source tree (`grep -r ... src`), never one file"
type: feedback
---

**Why:** because a unit rightly placing the fix in another module makes done refuse forever
