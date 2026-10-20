---
name: check-other-fleets-before-ordering
description: "before each order and each tick, compare the files of every open PR (`gh pr list --state open`, the desktop fleet uses fix/p4NN- and fix/p5NN- branches) with the files the order touches; merge the overlapping ready PR first"
type: feedback
---

**Why:** because two fleets push to one repo and duplicate orders waste units and collide
