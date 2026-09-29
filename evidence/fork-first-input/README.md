# Production fork first input

Deleted raw PI_PROMPT startup publication, one-shot --print launch arguments,
and delayed parent-session lookup/fork at send time. No compatibility path added.

Current production fork captures native history before registering/launching the
child, carries the selected model on Thread, and uses normal managed RPC args.
The existing fork task/explicit prompt remains one requested startup input. The
parent reserves that input in canonical InputDispositions for the launched owner
admission while retaining the wire lock. Worker startup takes only that exact
key, checks existing ReservedInput.queued_for authority, and dispatches through
normal OwnedTurn reservation/native binding. The wire lock excludes the worker
until registration and input persistence finish. Private raw PI_PROMPT rejection
is unchanged. Restart strips the startup key and never discovers/replays rows.

## Actual evidence

Installed noneditable wheel + matched complete native package:
physical Pi parent measured7888/32768 (24.07%), normal ThreadManagement.fork,
real detached worker and attached ACP message. Startup task+explicit heyBoss both
completed, zero native compactions, original parent file unchanged, exactly3 local
provider calls (parent, startup task, explicit input), no subsequent replay.
PASS1 in14.40s. Native local HTTP is real; no paid calls, synthetic native events
or live journals were used. Independent Dalton338 owns the committed test; local
run used his test with the preserved startup-task count and awaited real queued
completion before inspecting history. Early prompt() response only acknowledges
queuing, so a first premature observation is retained separately.

The first installed attempt uncovered obsolete --print and failed truthfully as
not_sent before a provider call. That causal RED is retained, source caller removed.

Shared false compaction trigger is independently ready PR340 head1f6bc389.
Do not wait for fork acceptance to install that correction. Live UNKNOWN rows
were never reset, replayed or modified. Parent owns live integration/install.

## Ownership

Exact archive refactor-audit.skill SKILL, pattern README and identity catalog
reread after owner correction; NRA skill reread. BOUND-2: use InputDispositions;
IMPL-13/IDEN-8: existing native helper and DetachedProcess process identity;
TIME-1: delete obsolete launch/send paths; IDEN-1: use existing ThreadIncarnation
and ReservedInput.queued_for instead of reconstructed boolean identity chains.
No FieldCodec subclass, parallel status store, native budget inflation or codec.
The actual native/ACP regression catches the removed launch failures. Independent
source audit/ratchet results appended when available; no CI wait.
