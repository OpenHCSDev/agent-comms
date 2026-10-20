---
name: wait-loops-never-match-themselves
description: "a wait or kill by process pattern brackets its first letter (`grep -q \"[v]erify\"`) or waits on a file or PID"
type: feedback
---

**Why:** because the loop's own shell command line contains the pattern and never ends or kills itself
