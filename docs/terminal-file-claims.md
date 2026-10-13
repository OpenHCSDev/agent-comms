# Terminal attempt file claims

Owner: core-goal-retry. Worktree: `/home/ts/wt/core-goal-retry`.
Branch: `fix/terminal-claim-followup` (draft PR #780). Retry/config/fork remain parked; thinking-level
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

NativeCoordinationSession declares the native boundary; StandaloneCoordination
selects SQL-only CoordinationSession regardless of bus-file presence. NativeBoundary
owns the wire/bus/registry locks through ExitStack. Callers explicitly lend that
resource lifetime through terminal SQL; no module writes ambient session fields.
Response publication, UNKNOWN failure and observed owner loss use the same boundary.
Release groups use recorded claim incarnations/generations and a defined #all notice,
not the first response route. Typed historical paths no longer use the current
project as their validation root. The old coding finish/release chain remains deleted.
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

#776's retained eight-check source checkpoint was published at 44703451, merged
and installed. Input 3327's interrupted old-runtime result was not replayed.

The coach requested a one-time live terminal-claim cleanup after backup. It ran
once before the subsequent shepherd review hold arrived: 54 exact terminal claims
released; 15 other claims retained. Included: core-awareness-index 6 (including
claim_admission.py), toad-close-lifecycle 13 and toad-ui-2 6. The tool was committed
at f7b7d724 and deleted after the run; there is no persistent second cleanup path.
Backup/result: /home/ts/.cache/agent-scratch/core-goal-retry/terminal-claims-backup-20261013
(owner core-goal-retry, purpose unreviewed cleanup recovery/evidence, size 4.1 GiB).
No further live cleanup is authorized by the current review hold.

Follow-up source is pushed in #780. Four terminal checks passed, including the
actual Python-worker/native stop/start/edit journey. That batch then timed out in
its separate selected-file writer. NativeCoordinationSession eagerly constructs
Registration; Registration.__init__ rereads the registry while that writer already
holds its lock. This is a new source defect, not a passing full batch. I own it.
The retained timed-out synthetic root is under followup-checks/ac-claim-read-jg3b0dox;
its operation was not repeated and its state is preserved pending disposition.

Seven corrected standalone coordination checks pass (1.28 s); seven owner-release
checks pass (16.39 s). The old silent-wire success fixture contradicted the actual
SQL response-finality rule: it now checks claimless success, while a separate wire
check confirms refusal and rollback. Four fresh scope checks pass: recorded
incarnation plus moved project, old terminal versus later generation, standalone
selection despite an existing bus file, and wire-success refusal. The retained
merged release implementation fails on changed incarnation; the new one releases
that exact generation in the same fresh isolated root.

Blocked edits: publisher.py and coordination_snapshot.py (single envelope-limit
owner), and coordination_session.py (constructor deadlock), each refused with
Resource already claimed by 'core-goal-retry'. No shell bypass or live rerun.
The shared OwnerLifecycle retirement implementation preserves distinct voluntary
and explicit-stop checks and captures the successor generation/thread together.

NativeBoundary declares one two-second acquisition budget shared across its three
locks; borrowed boundaries do not start another wait. SQLite busy waiting retains
the configured five-second default. These are not total completion guarantees:
registry construction currently performs an unbounded read, and disk I/O/fsync is
not deadline-limited. Fix the constructor ownership before claiming cancellation
is fully bounded or shipping #780. This follow-up is not merged or installed.
