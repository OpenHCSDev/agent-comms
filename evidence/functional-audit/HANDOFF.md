# Six-requirement functional completion audit

## Result and ownership

The six current functional requirements have actual-path evidence at the tested
scale. No new independent current-path implementation defect was reproduced.
One concrete activation omission remains: **PR94's scalable private source
checkpoint is absent on the active bus, and the current installer only supports
empty roots**. This prevents an unqualified claim that every merged PR94 feature
is active or that native-history proof remains available as this bus grows.

Parent owns live migration/activation and current deployment documentation.
Pascal owns the independent deletion-plan audit. This audit changes neither.
No provider calls, model changes, old input replay, live restarts, live store
writes or duplicate tests of PR173 were performed. PR173 was already published
and became merged during this audit.

Audit branch: `codex/functional-completion-audit-20260928`, base `b75d4f6`.
Installed baseline when audit started: runtime-relationship-cleanup-20260928
(core167/169, Toad85/86). Parent changed the launcher during the audit to
runtime-presence-20260928; its fresh source51/reply52 receipt says PRESENCE_OK
in 18.813 seconds. The old acceptance receipts below remain dated evidence,
not claims of a fresh paid run by this worker on every later package.

## Scope authority

- `/home/ts/.local/state/agent-comms/current-goal-objective.txt`, numbered outcomes
  1–6, is the current functional scope.
- `/home/ts/.local/state/agent-comms/pr17-pr94-completion.md`, current top table,
  provides the current checklist; historical paragraphs are not current status.
- PR17 and PR94 original bodies are retained as `pr17-body.md`/`pr94-body.md`:
  https://github.com/OpenHCSDev/agent-comms/pull/17 and
  https://github.com/OpenHCSDev/agent-comms/pull/94.
- Owner overrides require live default-on functionality, useful normal coding,
  all historical chats, no CI wait, no uncertain-input replay, no volatile
  worktrees and no extra paid models. Later no-compatibility/deletion directives
  are Pascal's parallel audit, not a substitute for this functional evidence.

## Requirement-by-requirement trace

### 1. Automatic bounded relevance triage and delivery

Evidence: `/home/ts/.local/state/agent-comms/admission-human-live-response.json`
(source23/reply24) and `admission-agent-live-response.json` (source25/reply26).
Each shows one subscribed running owner TRIAGE→FULL→response and the other
TRIAGE→IGNORE with no FULL input. Native session/entry identities and sealed
claim outcomes are recorded; these are stronger than queue/display ACKs.
`relationship-cleanup-live-response.json` source49/reply50 and the parent's
later `presence-live-response.json` source51/reply52 corroborate continued
unmentioned selected execution with normal read/bash tools.

Source: `wake.py:115` resolves member decisions; `ordinary_delivery_bridge.py:139`
records committed delivery; `coordinated_runtime.py:923` constructs bounded
triage, `:951` parses the exact bounded verdict, and the reserved-input path
`:1173` dispatches it. Native admission and sealed claims govern execution.
The preserved passive source10 and direct-mentioned source12 are separate
checklist receipts. Restored stopped identities staying stopped is consistent
with the newer requirement for idle **running** subscribers; it is not proof
that every archived identity was launched. Configured model settings are
recorded in `installed-observation.json`; neither running owner overrides them.

Assessment: evidenced for current running subscribers; no new paid test needed.

### 2. Complete integrated historical chats and saved sessions

The audit counted **8,420 preserved rows**: 8,400 public-source rows plus 20
previous private-root rows. #nra has 242 original rows; neither preserved source
contains a #openhcs post. The absence of #openhcs posts is not a migration loss.
`sources-inventory` details are in `source-inventory.json`; the source catalogs
and identities remain attached through `history_sources.json`.

Current installed normal UI acceptance: the guarded harness retained at
`/home/ts/wt/toad-historical-selection-20260928/evidence/historical-selection/`
reports exit0 in 38.263s, #comms/#nra historical display, saved UX transcript,
111 historical choices, and unchanged live bus sequence. The previous failed
runs were harness multiprocessing/Select-readiness defects, not a passing UI
receipt or a product selection fix.

The remaining breadth-of-session evidence gap was closed cheaply here:
**all 88 distinct referenced session files exist; all 88 latest pages and
available preceding pages read successfully through the installed parser**.
No page was empty and no cursor failed to progress. `saved-session-pages.json`
and `read_saved_sessions.py` retain the audit. They use the normal
`Comms.thread_transcript_page` implementation (`operations.py:1816`) with
registrations confined to a disposable owned fixture. Native files were read
only; no process/input/execution authority was restored. This validates bounded
readability and pagination, not full replay or full-file corruption scanning.
Identities with no recorded session are shown as such rather than given invented
conversations. The current live registry has 103 identities; historical source
registries supply 111 choices. The old shorthand that 96 restored identities
all had sessions should not be used as a file count.

Assessment: normal UI plus all referenced saved-session parser coverage; no
new migration/product patch indicated.

### 3. Launcher, feedback latency and truthful native-history status

The actual `~/.local/bin/toad-comms` resolves to the existing
`/home/ts/.agent-comms/stack/bin/toad-comms`. It follows the installed ACP launcher,
clears obsolete root/native environment pins, resolves the selected registered
project, and invokes normal Toad ACP. Prior PTY acceptance remains in
`/home/ts/.local/state/agent-comms/history-live-pty-check.txt`.

`/home/ts/.local/state/agent-comms/admission-ui-timing/result.json` measures a
mounted installed headless path: feedback 0.098s, sent-message paint 0.965s,
reply paint 9.803s. This is one observed sample, not a p99 or an X11 measurement.
Latest actual provider timings vary (source49 ~22s, source51 ~18.8s); they do
not establish that submission feedback regressed. Source `turn_progress.py:104`
emits uncaught execution errors once through existing ACP events, called by
`owned_turn.py:851`, while preserving error propagation/cleanup.

Native-history contention/wording fixes have PR124/Toad72 local and installed
mounted/PTY receipts in the checklist. **The scalable cursor activation omission
below limits the long-lived history-proof claim.** It does not prove current
selected delivery is broken.

### 4. Normal coding tools and cooperative N/K resource claims

`evidence/channel-coding/installed-coding-20260928-result.json` records actual
installed isolated FULL completion, exact edited/created file contents, released
claims and CODING_TOOLS_OK. Its harness asserts resulting files and claim release;
the result alone does not enumerate which tools ran. To close that evidence
weakness without spending another provider call, this audit read the retained
native transcript: **read, edit, write and bash each have an actual tool call and
matching successful tool result**. See `existing-coding-transcript.json`.

`private_nk_entrypoint.py:65` returns normal production selection without an
inferred one-file proof intent. `coordinated_runtime.py:1299` and `:1314` select
normal FULL coding; TRIAGE remains no-tools. Existing channel_coding_tools and
claim-envelope owners mediate edits/creates, and the retained receipt proves
exact-generation release after success. Bash/external editors remain the
explicit cooperative boundary described in proposal §7; no universal filesystem
sandbox is claimed or added to the six-goal scope.

Assessment: all four tools and claims have actual evidence, not only a prompt
request or model success statement.

### 5. Merge, install and real activation

Current checklist receipts establish the normal launcher/backend/history fixes,
source49/reply50 on core167/169 and Toad85, and prior queue/coding/native failure
acceptance. Parent source51/reply52 confirms the later runtime-presence install;
`installed-observation.json` records the observed launcher, owner configuration
and compaction settings. Parent owns later combined package acceptance; this
worker did not restart or retest paid owners. No CI status was used as a gate.

Assessment: the baseline is genuinely installed/used. The checkpoint exception
below means "every merged optional PR94 path is activated" is not established.

### 6. Default-on PR95 compaction with queued followup

`evidence/input-drain/actual-provider-queue-result.json` and the later
`evidence/s7-integration/real-queue-result.json` are existing actual-provider
acceptance. The harness `evidence/input-drain/real_queue_acceptance.py:154–200`
asserts durable accepted_not_started while summary is in progress, linked summary
attempt, committed native compaction, exactly one native start for each original
and followup in order after the compaction, exact original reply, all four
facts in the followup, and no remaining UNKNOWN/reserved attempts or ACP errors.
The temporary queued row initially being UNKNOWN is the durable unstarted
representation, not an assertion of a completed uncertain native send; the
terminal assertions check that it becomes started exactly once.

Earlier six-turn/three-summary retention is recorded separately in the current
checklist and retained PR95 receipts. Installed source invokes
`maybe_compact_owner_turn` from `owned_turn.py:717` before original send. Effective
native SettingsManager defaults compaction to true (`dist/core/settings-manager.js`
getCompactionEnabled); neither global settings nor either running owner's
project sets a disabling override (`installed-observation.json`). Native package
is the same diagnostics package recorded in the successful S7 queue acceptance.

Assessment: default-on and real repeated/queued acceptance are evidenced.
No old UNKNOWN or the earlier failed queue harness was rerun.

## Concrete remaining gap: scalable private cursor activation/migration

Read-only active-root observation (`source-inventory.json`): bus has 52 rows in
sequence, 253,675 bytes; bus_meta.json has claim_envelopes_version=1 but no
checkpoint_version; private_bus_checkpoint.sqlite3 does not exist.

Current installed `native_source_cursor.py:31–56` selects the old bounded bus
fingerprint without that checkpoint. `proven_source_coverage.py:23–25,110–126`
retains the 8MiB/1,000-row canonical scan refusal. PR94's merged scalable
certificate path is therefore not active. `private_bus_checkpoint.py:270–287`
explicitly refuses nonempty roots, so changing a flag is not a valid activation.

Required follow-through belongs with the parent's live migration ownership:
provide and locally verify lossless existing-root checkpoint installation, then
activate through the serial deployment procedure, retaining current bus bytes,
sequence IDs, root identity, SQL dispositions and UNKNOWN inputs. Reuse the
existing checkpoint owner/schema, validate the existing committed prefix, and
retain refusal on incomplete publication; do not start a new bus or replay old
inputs merely to qualify as a fresh root. Tests already in
`tests/test_private_checkpoint_cursor_integration.py` cover fresh-root large
history, not existing-root migration. This audit did not mutate the live root
or expand into an overlapping cutover implementation without a handoff.

This is a concrete capacity/activation limit; current ordinary traffic is below
it and the existing small-scale live receipts remain valid. It is distinct from
native proof-journal lifetime growth, already explicitly tracked by open
https://github.com/OpenHCSDev/agent-comms/issues/107. That owner-requested issue
exists; no duplicate issue or silent claim of its completion was created.

## Original proposal limits that must remain explicit

Proposal §§3–4 explicitly mark owner-authored decision supersession, arbitrary
DM obligations and task→commit heads as future features. The current optional
awareness projection includes selected claims and their execution obligations;
it is not evidence that those broader future features shipped. No new feature
scope was invented here. Likewise the existing 10/150-thread producer/index
benchmarks do not establish full provider/UI delivery p99 or sustained injected
size; the old PR94 body explicitly distinguishes those metrics. The six-goal
live acceptance should not be advertised as that broader performance proof.

## PR95/new-feature debt: explicit follow-on witnesses

These are source ownership findings for `plans/POST-FEATURE-DEBT.md`, not a
claim that large modules malfunction or a reason to disable working compaction:

1. **Compaction state decisions are recovered outside a declared lifecycle.**
   `compaction_journal.py:61–118` stores operation/selected-summary/publication
   status as strings; `_TERMINAL` is a separate set. SelectedSummaryAttempt
   interprets linked/declined-prestart at `:87`, while
   `selected_summary_admission.py:171,230` recovers the same state distinction
   and owner_compaction_commit.py:520,636 interprets committed outcomes again.
   Follow-on should extend the existing journal-owned state boundary with the
   established declared lifecycle/FieldCodec owners, migrating all consumers
   and removing these repeated state rosters. Preserve independent native
   commit evidence, one-use returned acknowledgments, durable UNKNOWN and the
   selected-summary/original ordering; a generic status enum would not fix it.
2. **Native preparation travels past its decode boundary as an untyped witness.**
   `owner_compaction_prepare.py:68–73` stores `witness: dict[str,str]`, validates
   its keys at `:145–159`, then `owner_compaction_commit.py:218–258` accepts a
   plain dict, copies/serializes it and passes it to downstream native ownership
   checks. The native writer's independent CAS must remain. A follow-on can
   establish a typed preparation/witness at the actual external decode and
   migrate internal consumers to it, while retaining native validation and
   saved intent formats. This is a concrete typing/ownership seam, not proof
   of incorrect current commit behavior.
3. **The native proof-journal lifetime limit is already tracked by issue107.**
   It is different from both core CompactionJournal and the private bus
   checkpoint gap above; neither module splitting nor bigger constants closes
   it. No new issue or lifetime-growth proof is claimed here.

Parent's updated C0-carve/POST-FEATURE-DEBT plans were read. C0 declaration
regions/MessageBus/presentation ownership is **assigned, not implemented**.
The next task is a current dependency/consumer graph and file sequence; shared
declarations edits wait for Pascal's typed turn-lease closure. Pascal owns the
operations composition, parent owns paired Toad import migration and deployment.

## Cleanup / publication

Owned temporary registration fixture was removed after the read audit. Retained
only scripts, receipts and source/proposal references. No deployment/status doc
owned by parent or deletion audit owned by Pascal was edited. The checkpoint
finding was reported once during the audit; parent can assign that migration
surface separately while continuing current service.
