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

## Working source checkpoint

RetainedNative.reload now conveys the existing NativeIntent reconciliation and
CommittedNativeOutcome, plus the original declared reason. It no longer sends
SwitchSession. The existing SessionManager reopens its real file and delegates
marker/digest/ancestry/fsync evidence to reconcileCompactionCommit; exact observed
receipt equality is required. No old-cut invalidation is reset. AgentSession
owns the shared context installation and session_compact event for manual,
automatic and known external compaction. Its original compaction controller
holds the hook lifetime and refuses concurrent input. Selected settings and
summary currentness now derive their idle decision from this same session.

Arendt589 SessionContext conversion/read-only projections remain untouched;
this calls original SessionContext.restore. SDK input claims and extension
bindings remain on the same session, as with normal SDK compaction. Actual
SwitchSession retains full replacement semantics. No store/schema/policy change.

Six production files; the projected compiled AgentSession deletes28 lines and
adds32, SessionManager adds15. Python deletes9/adds35; native summary helper
deletes10/adds3. Patch header offsets are packaging metadata, not production
deletion. Four direct test callers now supply their existing manual reason.

After Python AST:726 modules, zero omissions. Native before:268 modules, zero
omissions; changed native source:3 projections. Acorn syntax checks pass. Full
immutable native artifact qualification is pending Sch's build: source-only
reconstruction retained negative anchors from two unrelated later-modified
hunks, and trailing context was corrected for both complete event deletions.
These are not an installed-native pass.

Reused534 normal wheel:311 Python members byte-equal, command encoding roundtrip
passes. Initial abstract response declaration was corrected to existing
EmptyData; no new response facade. Four original installed worker/cancellation
controls pass in1.23s. No native/provider input was sent for these controls.
Matched native known-commit/hook/stale-cut/cancel/next-input qualification remains
required. Original13.562/98.141 negatives and original581 actual source/proofs
are preserved. This checkpoint is not Ready and makes no speed claim.
