# Known commit reload retains the original SDK runtime

Mendel owns the original post-summary lifecycle. #591 is merged independently;
this follows its existing checkout. Native2b original268 JS modules parse with
zero omissions; Python before census covers726 modules/zero omissions. Dynamic
extension hooks and JS held in strings are not syntax proof. Parent/Arendt gave
exact method claims: SessionManager reload/reconcile, AgentSession manual/auto
context-install and compaction event blocks, nativeRPC/RetainedNative.reload.
Arendt589 EntryStore/SessionContext/TurnContext source-view methods untouched.
Sch alone owns immutable build/pins; public cutover remains parent-owned.

Current owner fact: RetainedNative.reload sends SwitchSession for the SAME known
committed native identity. AgentSessionRuntime.switchSession opens a new manager,
aborts/shuts down/disposes original session, rebuilds runtime/services, then host
rebind/session_start/resources_discover. Native normal compaction instead updates
its existing manager/context and emits the real session_compact hook. These are
distinct source-update vs session-replacement lifetimes. Keep real replacement
behavior for actual SwitchSession; remove its use for this known commit.

The original manager can reopen its exact saved source with setSessionFile and
reconcile the original intent via reconcileCompactionCommit. The latter owns
commit marker/payload/metadata/ancestry/writer/durability checks; no copied marker
checks or success flag. Require exact known committed position from original
journal before installing context. Old EntryStore closes on replacement; existing
awaited source projections must refuse their closed/stale cuts, not reattribute.

Target: the existing AgentSession owns one compaction-context/install hook for
normal manual,automatic and original known external commit. Keep original model,
settings,auth,tools,services/runtime,source/proof/input ownership separate. The
original typed NativeQuery conveys the actual intent and known receipt to the
already acquired idle child. It grants neither mutation/input admission nor
UNKNOWN reconciliation/retry. UNKNOWN/refusal/cancel still retire original child.
Original writer fence and original post-read identity/revision checks remain.

No reproduction/provider/new environment/worktree/native copy. Source checkpoint
and normal build go to Sch after coherent changes. Final changed installed native
path uses existing holder; original negative 13.562/98.141 spans retained.
No attribution that teardown dominates those spans without original timing.
Patterns IMPL-12/shared lifecycle; old resource authority remains canonical.
