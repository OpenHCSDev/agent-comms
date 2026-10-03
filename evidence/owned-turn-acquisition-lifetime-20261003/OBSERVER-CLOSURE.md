# Complete acquisition / observer source batch (working, not Ready)

416 production lines deleted / 572 added across 26 existing files,
against original #599 base22637139. No new production class/store/queue/schema,
native change, provider selection or timeout. Patterns IMPL-12/13: one joined
resource lifetime and original owner decisions replace synchronous loop paths.

## Original fact / owner / consumers

- InputDocument.record decides reservation membership. OwnedTurn.begin always
  asks InputDispositions.record; its copied full-document membership read is
  deleted. The original rollback keys are enlisted before recording bus input.
  InputDispositions.record enlists supplied ExitStack custody inside the update
  before publication; QueuedInput.capture supplies it. reserve_turn returns the
  exact LockedStore.update document through SingleInputBatch. Existing originals
  remain untouched; finish_unbound keeps UNKNOWN/Started and changes only known
  never-started input.
- GoalScheduler owns READY preparation, set/edit/control/Retry and notification.
  Coordination.run_worker joins registry/goal/ledger operations. Its existing
  GoalScheduleCheck owns loop task/inbox/wake/queue admission before and after
  the worker wait; original native owner idleness and READY grant checks remain
  in the storage owner. ScheduledTurn binding and WakeScheduleCheck stay on loop.
  A READY result grants no native send: OwnedTurn rechecks original authority.
- InputDrain joins observer snapshot/diagnostic/wait recovery, input receipt
  reads, and acceptance/rollback. Queue binding, revision, edits, controllers and
  emission stay on loop. Original async store-lock acquisition preserves the
  same wire exclusion during queue edits/worker receipt capture. No blocking
  reacquisition is introduced inside it. Workers close their own JSON/SQLite
  resources before return. Following input rechecks its exact live inbox after
  awaited capture; retired inbox refuses handoff and original custody settles
  NotSent. Pending wake work is not popped before its joined observation returns.
- ConfigOptions joins registry/options and original configuration producers;
  SessionLifecycle joins declare/load/attach/native configuration/metadata and
  original owned retirement. Its connection binding uses the same RegistrySnapshot
  as RuntimeRequest.bind, including aliases/status; no second registry read.
  SessionLoadAdmission uses the joined worker rather than abandoned to_thread.
- RuntimeRequest and RuntimeConnection own original process/identity observation
  before/after socket acquisition. ProjectRuntimeRequest extends the same snapshot
  owner hook. Goal/input history, delivery, dismissal, goal snapshot and context
  RPCs join original owners; transport/subscriber binding and emission remain loop.
- TurnRunner joins compaction observation, cancellation, idle/followup/permission
  checks and relay peer observation; existing turn CAS/lease results remain exact.
  TurnProgress joins TurnGoalAccount provider/tool/terminal storage methods. Native
  InputStarted, terminal and ACP failure each consume one original InputDocument
  instead of rereading it per row/decision. CursorPublication joins its original
  scope owner without changing NativeSourceCursor.
- Original pending compaction publication keeps its independent nonblocking
  identity fence and exact pre/post handoff checks, joining registry/outbox work.
  Selected/manual preparation joins existing registry observation; no native or
  retained-context change. Original worker startup input and standalone foreground
  recipient admission/retirement join original storage with cleanup enlisted
  before callback delivery. _accept_visible_deliveries takes its existing Path
  resource rather than requiring an unused live SQLite connection from callers.

## Coverage and absence

Original NRA evidence726 modules/zero omissions retained. Additional current
production parser311 modules/zero omissions in async-observer-before/after.json;
changed source/test caller AST migration uses the existing NRA parser, no scanner
module/framework. The syntactic inventory names closures/worker callbacks rather
than treating their nested calls as direct loop calls. Receiver aliases/dynamic
MRO remain semantic readings, not an AST behavioral proof. All changed async
method callers, direct capture fixtures and helper Path callers migrate together.

New optional ScheduledTurn is an unbound work resource, not goal state; optional
ExitStack is operation cleanup custody, not input disposition. Goal/UNKNOWN/status
remain in their existing nominal owners. No domain state defaults were introduced.

## Preserved acceptance and remaining qualification

Installed01 retains actual worker-held-wire/observer failure and known NotSent /
lease cleanup. Installed02 seven controls passed3.20s at0d893cb5. Configured01
(one actual42MB saved SDK fork, original openai-codex/gpt-6.1-sol/high) finished
22.993s with exact user/reply, waiter released, lease idle, ACP/native exited and
original public source SHA unchanged. Both recorded PIDs now absent. This proves
0d source only; it does not qualify this expanded batch or historical latency.

Next: one batched changed installed check for cancellation/reservation, queue /
goal/socket observation and original refusal; then one distinct configured-fork
path on the final batch. No replay of configured01 or preserved UNKNOWN; no
534/live/default/native package mutation. Existing released540 code holder only.
