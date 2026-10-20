---
name: tests-bounded-and-backgrounded
description: "every test run goes in the background under timeout 300 with -o faulthandler_timeout=60, and any test process older than 5 minutes is investigated that tick"
type: feedback
---

**Why:** because a hung suite burned 50 minutes and a foreground gate froze the commander loop
