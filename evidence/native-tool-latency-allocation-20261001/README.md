# Ordinary native tool latency allocation

Arendt owns PR485. These observations perform original metadata reads only,
not prompts, native tool execution, owner restart or state repair.

The installed native0064 global-project-sync extension awaits a synchronous
agent-comms thread --no-pending child before every managed tool except the
project-change command. That callback runs in AgentSession.beforeToolCall,
outside streamAssistantResponse and its model-request callback counters.

Three actual installed CLI reads took **539.32, 547.60 and 553.35ms**. Registry
and wire revisions remained unchanged across those reads. The separate profiled
read attributed 532.28ms cumulatively to package initialization and 129.96ms to
CLI main, including 93.34ms to constructing the wire. These spans overlap and
profiling adds overhead; they must not be added or subtracted from historical
tool intervals.

This is a measured local subprocess cost on every managed read, not evidence
that all historical 0.7–2.3s journal gaps have the same cause. Native preparation,
execution/result hooks and Core event consumption remain separate boundaries.
The existing project handoff check must survive removal of repeated CLI startup,
using the canonical owner/registry authority. No cached worktree or second
admission authority is permitted.

Separately, the original 120.935s model request records first consumed delta at
36.423s and callback total 145.753ms. Its 95.5% prompt-cache hit does not establish
provider queue, reasoning or generation attribution. The model-request observer
does not time subsequent tool callbacks. The older 129.898s journal gap remains
unallocated at that strength.

The latest saved 3.818s request records first event at 309.53ms and first consumed
delta at 3513.44ms, with 3.20ms measured callbacks. It is a request observation,
not a basic read timing. The latest NRA private observation is TRIAGE and must
not be presented as an ordinary tool turn.

Kepler's merged PR484 owns the separate inner prompt-binding acquisition fix.
This investigation does not duplicate that mechanism or hold its installation.
