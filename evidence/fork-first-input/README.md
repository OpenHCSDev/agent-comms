# Production fork first input

24 production lines deleted (including replaced lines): raw PI_PROMPT startup publication, one-shot --print launch arguments,
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


## Final ownership follow-through

Existing ThreadManagement.resolve_thread_model now resolves selection from the
already captured child; removed a new redundant absence check rather than
replicating its authority. Native helper accepts the already validated saved path
and worktree; it does not re-probe the parent Thread. Latest archive census:
no chain terms, codec subclass or foreign absence probe increases; each touched
Python file has foreign-probe delta0. Existing per-class ratchet has no positive
existing measure (also satisfies the stricter former class-size measure).

Full package-context NRA scan reporting native_fork/thread_management/owner_lifecycle/
worker produced0 findings. Its current JSON does not expose scan_status or omitted
counts; this is not a zero-omission certification. Existing real ownership plus
actual behavior evidence remains the basis for this correction.

Final installed fork rerun passed after model-authority/signature closure; receipt
actual-installed-fork-final.log. Focused private launch rejection/fence tests5pass,
14deselected; rawPI_PROMPT remains refused. No optional full-suite or CI gate.
