# C3 owner-state cleanup

Owner: Mendel. Receiving scope from Core419 C2, based on its tested 8c4afabe
checkpoint, now merged350b14a9. Source assignment: cleanup-2026-09-29.zip README/C0/C3. PR414 test
cleanup is merged; C2 is independently reviewable. No new workers.

## Full acceptance tracked here

Close foreign state reconstruction in all six assigned files:
coordination_response.py, owner_lifecycle.py, goal_actions.py, backend.py,
turn_watchdog.py and goal_management.py. Trace each probe to its actual declaration
owner and consumers. Extend existing typed identity, activity and command owners;
classify genuine optional/external boundaries rather than hiding them in wrappers.

Close all five family sites:

- goals.py mention resolution: states carry their own identity or diagnostic data.
- maintenance_barrier.py: decode one phase lifecycle at the boundary.
- relationships.py: declared add/remove/update commands own edits.
- thread_status.py: command capabilities own control eligibility.
- acp_failure.py: external payload shapes decode once and own detail rendering.

Arendt Core425/Toad211 now owns canonical TurnProgress/OwnedTurn backend state and Core/Toad
permission/status/cancel integration. Coordinate backend.py, goal_actions.py and
thread_status.py against that contract at its implementation checkpoint. No private
active_turn probes, reserved-flag projections or symptom branches. Independent
families continue while that contract is implemented.

## Authorities and exclusions

Parent replaced withdrawn Core418 checkpoint11748ae3 with4951d8d4.
The published API is agent_comms.field_codec.WireValue, inheriting
FieldRepresentation with abstract to_wire/from_wire and optional schema().
The existing representation owner handles encoding; no new codec or type switch.
No production code here depends on either checkpoint yet. Do not mark recursive
FieldCodec-on-self adapters. Scalar FieldRepresentation remains canonical.

Parent owns sealed.py and FieldCodec/PendingRequests/ReadLedger/PiRpcChannel/
ChildProcess seals, typed_table A13 review and C4. Einstein Core417 owns C1 Pi
vocabulary; pi_events.py remains excluded. Arendt explicitly approved the later
disjoint Thread report-field deletion; its identity builder remains his. Kepler owns TC2 ACP SDK.
Arendt Core416 owns large-context native accounting and usage acceptance.
Mendel's C2 registry_document refusal guard extension is already in Core419.

## Evidence, checks and deployment limits

Patterns: IDEN-3, IMPL-1, IMPL-3, IMPL-5, MEMB-2, BOUND-1, BOUND-2.
Re-measure actual heads; do not use the old 41-chain source snapshot. At2c1ca485
the six state files contain14 long BoolOp chains, five family files another6;
these20 are selected-surface screening leads, not a global count or20 defects.
Record per-function/file StringDispatch/TypeSwitch subjects and distinct arms.
Classify external taxonomies and enforce admitted family sites with guards.

Acceptance uses existing retained-native/ACP private continuous fixtures with
controlled loopback responses. Preserve original input/UNKNOWN/noReplay,
history/settings/correction and owner custody. Source tests, installed entrypoint
acceptance and global activation are reported separately. No paid provider calls,
live-root mutation, global install or owner restart. Scratch owner Mendel:
`/home/ts/.cache/agent-scratch/comms-owner-state-cleanup-20260929`.

Independent obsolete selected-summary child fixture cleanup is a separate
tests-only follow-up; no production compatibility export will be added.

## Tested source checkpoint

Independent families implemented: relationship commands, maintenance lifecycle,
goal mention contact/diagnostic projection. Response publication admission,
preparation and frozen-intent checks now consult the existing RecoverySnapshot,
execution, attempt and obligation declarations. Stored mention keys remain the
current format; unresolved members declare constant absent identity fields.

Serial evidence: response-goal-maintenance-final.log has47 passing checks in7.95s;
relationship-family.log has19 passing checks with3 resource-heavy concurrency
cases deselected. These are source checks, not full C3 native acceptance.
Remaining files and both remaining families above stay assigned here.

The concrete cross-runtime restart queue blocker is a separate C2 followthrough
PR: exact source identity proof and reviewed target runtime must be distinct.
No live queue activation belongs to this source checkpoint.

## Further tested owner operations

External error shapes use existing MroDispatch declarations at one decode
boundary; internal published receipts still use FieldCodec. No custom WireValue
is needed.22 presentation/diagnostic checks pass. Goal/GoalState own failure
blocking and provisional-completion revocation; owner pause, progress and exact
goal/worktree gates remain.61 durable checks and23 focused block/resume checks
pass. Eight fake-stream caller cases require public RequestError expectations,
reproduced on main697b plus Core424; Arendt425 has the caller-closure evidence.

Actual saved native history -> normal ACP input -> autonomous native503 -> typed
published failure receipt -> blocked goal -> passive reopen/noReplay passes in
18.67s (actual-goal-failure.log, native7817). UNKNOWN original remains unchanged.

OwnerReleaseReceipt owns source/admission release proof; RegistrySnapshot owns
fenced stopping identity and delegates executable process proof to Thread.
33 release/process/turn-lease/restart/error checks pass28.07s, including actual
release-before-exit, grace/escalation and stale birth/admission/missing receipt.
The obsolete expected_incarnations fixture now uses OwnerRestartSelection.
A redundant retired startup refusal sentence was deleted; actual private trace,
PublicationActivationBlocked type and owner identity assertions remain.
Watchdog reads existing NativeAttestation.observed rather than reconstructing
attestation from a nullable state field.

Remaining canonical turn/activity/control consumers await Arendt's reviewed425
contract; f05a62e7 is an importable source checkpoint, not full acceptance.
The owner prioritizes the separate sender outbound relation contribution to
Schrodinger210. C3 full acceptance remains tracked here, no scope discarded.

## Current main remeasurement and whole-row closure

Normally merged main7fcf826a atf522f722. Parent432's current C0 screen remains
the source ledger; older chain counts above are historical evidence only.
Goal.state resolution is already removed from main, which does not close the
separate GoalMentionBinding.__post_init__ seven-resolution validation row.
This branch replaces that validator and its relationship projection consumer
with declared mention members. Authoring, stored decode, renamed/deleted peer
incarnation, diagnostics and contact projection stay in that same workflow.

Both relationship action sites belong to this PR: RelationshipDocument.edit
and ThreadRelationships.edit. The former string-dispatch method is deleted;
declared RelationshipEdit members own changes through RelationshipEditContext.
The latter accepts the declared command, with tools and all source tests migrated.
No parallel string entrypoint remains. The external tool wire spelling is decoded
once through the existing FieldCodec family representation.

ACP _error_detail and maintenance current_unlocked raw record/phase screening
sites remain full assigned rows: external error data decodes through the existing
MroDispatch boundary; MaintenanceMarker/State decode through FieldCodec and the
phase lifecycle owns admission. Neither closure is a renamed raw-shape wrapper.
All consumer removal, admitted-site guards and actual paired acceptance remain
required before claiming full C3 closure.

ThreadStatus.allows_control is still open. Its actual cross-project consumer is
Toad ThreadAction.available, which passes the tool name. Extend the existing
declared command capability and migrate that consumer with Arendt's canonical
turn/permission/status contract. Do not build another control roster, status
copy or private active-turn projection. Backend and GoalActionContext turn
consumers also remain assigned here, with shared edits coordinated to Arendt425.

Tests-only422 retains the obsolete selected-summary child fixture and the
separate selected-native injected-original versus coverage gap. It is not
closed by430 source/handling sanity checks. Current priority430 contributes
canonical source identity and original-recipient handling to Schrodinger215;
native acceptance is serialized after425 and431, without replaying failed input.

The declared control seam is now implemented here and paired in Toad216, in
/home/ts/wt/toad-declared-owner-controls-20260929. Arendt approved only this
capability and ThreadAction.available consumer change. ToolRequest declares
availability; OwnerLifecycleControl is a shared nominal capability, with start
owning its distinct start query. Existing ThreadStatus members own lifecycle
eligibility. Deleted all three allows_control string-policy methods and migrated
the actual Toad consumer to its declared command. No policy roster, stored flag,
status/turn mirror or competing turn model was added. Both projects' current
source scans contain no allows_control callers/definitions.

The existing Toad thread_action_deletion_pilot guard now rejects that retired
consumer too. Its real introduction cccbe4aa created the file and grew the scoped
attribute witness from0 to1; candidate0. control-guard-history.json in Toad216's
named scratch records this exact scope, not a global historical debt claim.
63 focused status/family checks pass4.22s after main integration; the actual Toad
menu declarations consume the paired capability for archived/running/stopped
states. These source checks do not prove mounted/installed menu or native turn
readiness. Those acceptance obligations and six-file turn consumers remain open.

## Canonical turn consumers and goal-report mirror deletion

Normally integrated main72062939 (merged425 and431) atf524e42b. Watchdog uses
TurnSession.awaiting_native_attestation; that session capability and all three
backend callers delegate NativeAttestation.observed. Goal reports use the public
TurnState.report_turn. Standby captures its original Thread.turn_lease rather
than reconstructing the reporting identity. Wait graph and idle recovery read
the public turn projection. Terminal wait release compares the original
FinishedTurnFence with Thread.observed_turn under current admission, preserving
rename, birth, generation and stale-callback rejection.

Deleted9 production lines implementing the competing report mirror: Thread's
last_goal_report_turn field and validator (5), plus GoalAction's read/write sites
(4). The once-per-turn gate now derives committed Goal.reported_turn revisions
from the existing goal_history owner for the original thread incarnation. Clear,
replacement and cold reopen cannot erase the historical report. No new store,
report flag, seen list or compatibility field was added. Arendt approved the
disjoint Thread deletion alongside his AdmissionIdentity builder work.

Extended the existing mutation guard to forbid the retired report field and
ThreadStatus.allows_control callers. Real history0c94f4ce introduced six guarded
AST occurrences across declarations.py/operations.py; the prior head has zero,
and today's moved Thread/GoalAction declarations have zero. Named scratch
goal-report-mirror-guard-history.json traces the actual original paths rather
than falsely counting a later file move as debt introduction.

58 focused source/owner controls pass (56 in7.86s plus two correctly configured
private socket/scheduler controls in1.39s). Four fake-stream standby tests fail
identically on unmodified main72062939: their streams omit actual InputStarted,
so the public RequestError correctly retains an unstarted original. One also
uses an obsolete emission callback signature. Baseline4FAIL2.64s is retained
in422 scratch; these are fixture migration debt, not production compatibility
requests or installed acceptance.

The removed registry field is a hard format cutover. Parent owns the quiet
cutover: preserve registry/native history, original input/UNKNOWN and committed
goal_history. If an old active turn's only report evidence is the retired field
without a committed journal revision, do not erase its report guard or fabricate
a journal receipt; complete the existing idle boundary before format conversion.
No old-field reader is added to production. Paired421/216 actual installed goal,
menu/control, release and noReplay acceptance remains pending serial native slot.
All five family rows and all six assigned consumer files remain tracked above.

Normally integrated main433 ea7cbaed at47210dfb, retaining its AdmissionIdentity
declaration and public callers together with this branch's typed refusal guard.
All five affected families pass78 focused source checks in8.12s: goal mention
resolution, both relationship edit layers, maintenance lifecycle, ACP failure
boundary and declared thread command capability, plus the existing deletion guard.
The full six-file backend/goal/owner/watchdog consumer and paired216 installed
acceptance remain required; this source result does not close them. Urgent430/215
hot source/read-frontier and434 writer correction take the next coherent native
journey. No global runtime, registry format or saved journal was changed here.

## Current C3 deletion and complete consumer census (2026-09-30)

Normally integrated main439 at23c417f2. Delete the entire66-line goal_pauses.py,
its duplicate writer and adapter event, Goals.pauses/goal_pause, the redundant
HistoryViews.list_threads goal_pause key, and the unused passive failure pause
argument. Current Goal.state carries pause source and its declaration owns model
resume eligibility. The original goal_history journal preserves every committed
revision. GoalPrecondition now carries the declared expected GoalState instead
of comparing status strings; resume uses the original complete Goal CAS. Original
Thread.executing supplies dependency/idle queries, not managed-id absence tests.
All production and test consumers were searched; no retired pause-store import,
writer, reader, method or duplicate failure parameter remains. Toad216's existing
owner-pause pilot now reads the same original Goal, with no adapter.

Delete the obsolete pause-store golden test and injected second-store failure
case. Existing journal failure/crash/UNKNOWN controls remain. Eight synthetic
backend-stream failure cases are replaced by the existing retained-native/ACP
failure journey, now also pausing through the actual owner command at the held
localhost response boundary. EOF/cancellation remains covered by the existing
actual native lifecycle control; observation abort/sync/crash controls remain
with the actual attempt store. No production compatibility mechanism is added.

Repeated mirror evidence extends the existing mutation guard. Its scoped pause
witness grows0->7 at original3cdc0569 and is zero now; the preceding report-mirror
witness remains enforced. Evidence: goal-pause-mirror-guard-history.json. This
is a declared-owner guard extension, not another audit framework.

### All six required consumer files

| File | Canonical authority and remaining true boundary |
| --- | --- |
| coordination_response.py | RecoverySnapshot, execution/attempt/obligation members and original RegistryOwner own admission/finality; absent SQL/source proof is a real rejection boundary. |
| owner_lifecycle.py | OwnerRestartSelection and OwnerReleaseReceipt own birth/admission/process custody; stopping obtains Thread.require_process. Optional reviewed launch and explicit environment/argument inputs remain genuine caller boundaries. |
| goal_actions.py | Current Goal owns pause/transition data; committed original goal_history owns the once-per-turn report. Optional private owner grant and requested CAS fields remain explicit authority inputs. |
| backend.py | Existing NativeCustody and NativeAttestation own native resource/reopen/readiness. Optional actual asyncio pipes/tasks/listeners remain resource boundaries. The _maintenance_wire_locked callback marker remains an OPEN shared custody row. |
| turn_watchdog.py | TurnSession delegates attestation to NativeAttestation.observed; watchdog clocks and historical irreversible-work observations are bounded runtime resources, never input/goal/retry grants. Optional finish tasks/deadlines remain real runtime bounds. |
| goal_management.py | Current Goal, committed journal, original input/disposition and GoalWait owners supply reports, reviews and dependency state. Thread.executing and original finished-turn fence retain rename/birth/admission/source custody. Optional cleared goals and absent exact wait/turn evidence remain real boundaries. |

Arendt explicitly owns the remaining send-custody marker closure across initial
backend send, forwarded turn_inputs and OwnedSendAdmission AFTER the frozen
61506 paired gate. Keep that row open here; do not add a competing boundary,
private active-turn probe or custody boolean. All five family rows remain in
this PR: mention resolution, BOTH relationship edits, maintenance lifecycle,
ACP error shape boundary and declared control availability. Parent's C4 builder
is untouched except the approved obsolete list_threads pause projection.

### Durable cutover and acceptance limits

Preserve existing goal_pause_events.json as historical evidence; do not erase it
or read it as current authority. No live file cleanup or new store is required.
Preserve registry/native bytes, goal_history and UNKNOWN. The exact original
14C0 last_goal_report_turn carry/idle rule above is unchanged, including retained
snapshots and any unjournaled active report. Parent owns quiet cutover; no legacy
reader or fabricated journal receipt is introduced.

89 focused source controls pass (85 in4.24s plus4 recovery checks in0.84s
with the explicit native-package fixture environment).
This is an in-progress source checkpoint. Paired216 installed goal/menu/native
acceptance is required next; source tests do not close C3 or authorize activation.
Scratch ownership and preserved original proofs remain as declared above.

Installed20079652 wheel + reviewed native615 now passes the two retained native
ACP goal failure journeys in31.95s. One preserves failure/UNKNOWN and permits
only explicit Retry/new continuation; the other pauses through the actual owner
command at the held localhost response, preserves that Goal source and journal
through cold reopen, and remains passive. Original native history is a retained
prefix; the earlier unrelated UNKNOWN row is unchanged. Native/owner resources
are retired by the existing fixtures. Sanitized evidence is in
`evidence/c3-goal-owner/native-receipt.json`; full private originals stay under
the declared scratch run02 root. Actual deleted production lines:104, added22.
This is installed Core/native/ACP acceptance, not completed paired216 mounted
UI acceptance. The first mismatched native614 preflight was rejected before
launch and is preserved. No production restriction was bypassed or reset.
