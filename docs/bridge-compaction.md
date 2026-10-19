# Claude bridge compaction

Owner: pi-bridge-compact, branch `fix/bridge-compacting`.

Requested acceptance: a real Claude Code `/compact`, a running bridge on a
private scratch bus, and one-second `agent-comms threads` observations before,
during and after. No timeout changes, UI guesses or name-specific handling.
Open a PR only; do not merge or install globally.

Investigation: trace bridge lifetime, watch claim, transcript observation,
registration and status presentation before changing their owners. Existing
`manual_compaction_bridge.py` drives native Pi compaction and does not observe
Claude Code's hooks. The existing stdlib-only `agent_comms_hooks` declarations
and `WatchClaim` identify a Claude session's live watch.

Scratch ownership: `/home/ts/.cache/agent-scratch/pi-bridge-compact` is owned by
this task for private bus, real Claude launch output and bounded proof logs.
Claude's original transcripts/auth, live bus and uncertain attempts are preserved.
The requested checkout already exists outside `/home/ts/wt`; no new worktree is
created. New verification source and handoffs stay in this persistent checkout.

Resource check: `python tools/agent_resource_check.py --assert-headroom` reports
warning (4.6 GiB available RAM, 3.9 GiB free /tmp). Run one small session and one
bridge sequentially, reuse installed dependencies, no fleet or large suite.
The documented `/home/ts/bin/agent-resource-check` is absent on this host.

Implementation, source ownership evidence, real proof and final checks pending.
