# Terminal attempt file claims

Owner: core-goal-retry. Worktree: `/home/ts/wt/core-goal-retry`.
Branch: `fix/terminal-file-claims`. Retry/config/fork remain parked; thinking-level
checkpoint remains separately published in draft PR #773.

## Source and reproduced cause

At base `23f4699cf`, `tracked_turn.py:553-554` invokes tool-mode finish only
after terminal proof and successful response validation. `channel_coding_tools.py:
121-135` maintains a separate live-only release operation;
`claim_admission.py:230-271` demands the original selected wake remain live.
`coordination_snapshot.py:214-315` terminalizes attempts without releasing files.
A private synthetic root reproduced a failed terminal retaining the exact
acquired file claim. No live root, saved user session, UNKNOWN input or goal was
changed. The separate UI/database hypothesis is unrelated to this assignment.

## Owner and consumer move

RecoverySnapshot owns terminal settlement and now derives the file set and exact
generations from certified durable claim envelopes, joined to its AttemptRecord
(execution, ordinal, recipient lookup, participant generation, incarnation).
It releases before either nonpublication terminal mutation or final successful
publication mutation. Manual unbound claims and claims of later attempts are not
selected. Publisher retains typed ClaimRelease generations instead of substituting
whatever generation happens to be current.

CoordinationSession borrows the existing wire/bus/registry boundary through
terminal SQL commit. Its temporary tuple contains acquired resources, not copied
claim or attempt state. Response publication, live UNKNOWN failure, explicit
released-owner recovery and nonpublication settlement use that boundary. Bare
coordinator stores without a bus have no file claims to release. The old coding
finish/release chain is deleted, including its base hook and tests of that hook.
SelectedRequest now settles cancellation/abandoned local results through the same
UNKNOWN settlement operation after native child/tool cleanup. It never clears
input records/cursors or permits replay; unresolved frozen publication still
refuses settlement. Owner stop exercises this cancellation path before registration
release, rather than acquiring a second cleanup path.

Bus append and SQLite cannot form one atomic disk operation. As with existing
response publication, an append failure is UNKNOWN and is not retried. A visible
release can precede a failed terminal SQL commit; this does not recover an input or
provider result. Release occurs only after local finality checks; a revoked owner
continues to need the existing explicit recovery operation.

## AST evidence

Existing refactor-audit Package/ParsedModule tooling enumerated all 306 production
Python modules at the base, with zero parse omissions. Enumerated class bases,
fields/writes/checks, imports, release/acquisition and terminal call sites.
Relevant production consumers: AttemptStore.fail_unknown/settle_nonpublication,
RecoveryMonitorCapability.terminalize_dead_attempt, RecoverySnapshot.settle/
finish_publications, FullNativeSend.fail_unknown, selected actions/tool broker,
CodingToolOwner, TrackedTurnExecution.result, Publisher and Messaging.
String `release_selected_resources` has no remaining production declaration or
consumer after migration. Python AST does not resolve dynamic runtime monkeypatches
or native JavaScript tool execution; behavioral/native checks cover those paths.
This addresses TIME-1 (competing release path) and BOUND-2 (bypassed settlement owner).

## Checkpoint

Source implementation is published before final focused and actual-process
verification. Those results and any remaining gaps will be recorded here before
requesting acceptance. Disposable outputs belong to core-goal-retry under
`/home/ts/.cache/agent-scratch/core-goal-retry`; delete after recording results.
No runtime installation or relinking is part of this checkpoint.
