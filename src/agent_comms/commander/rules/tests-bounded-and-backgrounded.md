---
name: tests-bounded-and-backgrounded
description: "every test run goes in the background under timeout 300 with -o faulthandler_timeout=60, and any test process older than 5 minutes is investigated that tick, because a hung suite burned 50 minutes and a foreground gate froze the commander loop"
type: feedback
---

On 2026-10-18 the owner said: "do not ever ever run test sina lbocking manner ever. ALWAYS run in background". I had run the P47 head-vs-base gate as a foreground Bash call with a 660 s timeout, which blocked the commander loop and left the owner without replies.

**Why:** a foreground test run freezes the session; bus messages, the owner's questions and rotation events wait behind it.

**How to apply:** every pytest or runp.sh invocation (gates, reruns, single-file checks) uses `run_in_background: true`, and I keep working until the completion notification arrives. Include this in fork briefs. Related: , .

**Widened 2026-10-19 16:00:** owner, third time: "stop running long ass tool calls in the foreground." Not only tests: every command that touches the bus, order tools, gh, ssh, git fetch or an install goes in run_in_background. Foreground is only for local file reads and edits that finish in a second.
