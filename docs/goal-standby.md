# Standby while waiting for delegated work

Use `comms_goal(status="standby", wait_for=["@child"], progress="Waiting for the review")`
when an active goal depends on another thread's work. The goal remains active and
retains its identity, text, progress, and private attempt ledger. Its execution
state becomes **Standby**, distinct from paused, ready, thinking, and compacting.

The wait targets are explicit thread identities. Names are resolved through aliases
and bound to their creation identity, so renaming a child preserves the wait and
deleting/replacing it does not transfer wake authority. Goal text may contain
`@mentions`; editing that text does not create or alter dependency authority.

A new **direct message** from a declared dependency starts one goal turn with that
original message. An explicit user follow-up can also wake the goal. Channel
messages and unrelated threads do not automatically acquire this authority.
Unrelated direct inputs retain the existing visible UNKNOWN disposition.

If a dependency has already sent an undrained or UNKNOWN reply, entering standby
is refused with that message's sequence. Inspect it during the current turn;
UNKNOWN inputs are never automatically replayed. Wait consumption happens under
the prompt send lock after a goal attempt is reserved. A stopped/replaced owner,
changed wait, owner pause, or unavailable launch authority prevents forwarding.

Clients receive the typed `goal_execution` projection, also exposed as ACP
`_meta.agentComms.goalExecution`. It contains `state`, `goal_id`, and explicit
`wait_for` identities. The sidebar shows standby only when the thread is idle;
actual thinking, tool execution, and compaction take precedence. The separate
wait record keeps the persisted registry and activity schemas readable by older
local processes.

Standby does not resolve previously failed goals. Empty successful turns still
block an active goal; uncertain attempts remain unlaunchable and require an
explicit owner decision.
