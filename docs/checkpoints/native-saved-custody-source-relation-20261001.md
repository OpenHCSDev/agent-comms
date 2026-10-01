# #489 saved native custody: execution-selected source relation

Owner: Arendt. Receiving normal merge: `0c67fc64` (includes main #498
`9954cdd7`). This is a source/design and partial implementation checkpoint,
not Ready. The census fingerprints the working source, including the explicitly
unfinished selection/admission edits; its hashes identify that source independently
of the Git HEAD. No application, test, provider or public input was run.

## Original tooling and complete source context

[NRA census](../../evidence/runtime-lifecycle-semantic-pass-20261001/nra-custody-caller-census.json)
uses the existing `parse_python_modules`, `inspect_modules(findings=())`,
`SourceProductFlowRepository.class_index` and `resolve_function_call`. Python 3.14
parses all 311 authored production Python modules serially, without parse caches
or detectors. The existing NRA 3.11 environment cannot parse this source; the
analysis uses a 3.14 interpreter with the original NRA source API.

There are 28 requested Python root declarations, 60 declarations including
inherited implementations, 322 methods, 2,038 lexical same-name call candidates
and 1,917 projected flow candidates. NRA resolves 492 exact function edges;
the remaining 1,425 are retained with their possible symbols/open resolution.
Generic `.read`, `.prepare`, `.require` receivers are not falsely assigned to a
custody owner merely because their names match. Constructor targets and callable
references also require their original declarations; an unresolved edge is an
obligation, not evidence that no consumer exists.

`ContextBudget` and `CompactionPolicy` are native JavaScript owners, not missing
Python declarations. Their final serialized request and paused dispatch consumers
remain the native boundary portion of this same task. No Python substitute is
introduced. Native and pinned Toad event/wire consumers remain separately named
in the existing phase/replay receipt.

The recipe is `evidence/runtime-lifecycle-semantic-pass-20261001/runtime-custody-source-census.py`; it consumes NRA
records instead of implementing another AST visitor, dispatcher or owner registry.

## Required relation: questions, cases, source recovery

The OOPSLA source-fidelity obligation applies to every row below. Agreement
between a selected path, launch argument and decoded journal does not recover
the selection judgment. The captured original selector must reach the writer;
the emitted native identity must reach its proof and publication consumers.

| Question / cases | Existing determining owner | Source and consumers |
|---|---|---|
| What source was selected: new, freshly enrolled, saved, continued after a native result? | Existing `SelectedSession` behavior, selected from captured `RegistryOwner.thread` or explicit operator selection | `SelectedExecution` → `CoordinatedRuntime` → `SelectedRequest` → `PrivateSendAdmission` → `NativePiRpcLaunch.tracked` → `TrackedTurnSession`; no downstream location guess. A new member is justified only by saved-source behavior in this existing family, not by copying a session authority. |
| Which logical native session actually opened? | `NativeSessionIdentity` emitted by original native SessionManager | `StateData.identity`, `NativeAttestation`, `NativeCustody`, live selected attestation, original `NativeContextProof`, selected summary witness. Header and stat observations corroborate this fact; neither enrolls a fresh session. |
| Which child/channel/writer owns that session: empty, strict reopen, borrowed, retained, retiring? | Existing `NativeCustody`, owned by `PersistentPiSession` | `PiSessionChild` owns process/channel/stderr; `TurnSession` owns receive correlation. Launch-directory resources are distinct from selected saved location. Reopen must preserve the same identity selected by the original owner. |
| Did this original prompt cross the irreversible writer? | Existing `PromptAdmission` and `InputAttempt` / `InputDocument` | `PrivateSendAdmission`, `send_fenced_prompt`, `TrackedTurnSession` and native commit observation. UNKNOWN stays UNKNOWN; missing native context cannot become historical NotSent. |
| Which frozen messages belonged to this input? | Original `NativeInputExecution` plus `ExecutionAssignmentLink` / `TriageNativeSources` | Kepler #490 producers, binding and original native reservation, all historical/cursor/recovery and plural publication consumers. Membership is a join; no copied assignment in session identity. |
| What exact context was admitted? | Original `NativeContextProof` and live-recorded `NativeInputContext.reference` | `NativeRuntimeInput`, `NativeContextJournal`, prelaunch `PromptBinding`; historical/continued/recovery reads corroborate original rows, never mint context or permission from readable bytes. |
| What source prefix is covered and which cursor may advance? | `SourceCoverage`; eligibility/position owned by `CursorOwner` / `NativeSourceCursor` | Original sealed wire/audience/cohort, historical native evidence and live admission. Acquired `NativeEvidenceRead/Scope` owns byte lifetime and corroboration only; it is not a competing coverage decision. |
| Who is the current owner after fork/rename? | Captured `RegistryOwner` / original `Thread.incarnation` and current routing name | Launch/configured instruction contributors and tool sender consume the current owner; native header lineage and inherited summaries are historical. Sch #499 carries existing `CoordinationSegment` through all summary request/source paths, without another identity field. |
| Can an old released native attempt settle? | Existing `VerifiedOwnerLoss` / `RecoveryMonitorCapability` and `ReplayAssessments` | Original release/process/admission/frozen publication evidence. Recovery must observe the actual selected session, not infer process absence from a private parent path. Original UNKNOWN and native proofs remain untouched. |
| Does a saved source permit compaction/paused next dispatch? | Existing `SelectedSource`, boundary/held custody, `CompactionPolicy` consuming `ContextBudget` | Manual/adaptive/bridge, selected reservation, raw floor, prepare/commit/recovery; selected summaries are not commit permission. A lexical `native-sessions` parent cannot determine whether raw coverage is required. |

## Independent decisions being deleted, not renamed

The current working change removes these consumer decisions and carries the
same original `SelectedSession` object through their boundaries:

- `PrivateSendAdmission.session_dir` and `_saved_session`: delete rebuilding the
  location and fresh-versus-saved conjunction from store root + lookup + separate
  expected path/startup fields.
- `NativePiRpcLaunch.tracked`: delete saved-parent inference from independent
  `session_dir/session_file` arguments. Launch resources derive from the selected
  session; the saved-source selection is not repeated there.
- `TrackedTurnSession.attest`: delete private-parent reconstruction and separate
  current-path comparison; invoke the selected source's attestation contract with
  the original emitted `NativeSessionIdentity`.
- `NativeSendStage.verify` / `NativeContextProof.corroborates_input`: delete the
  parent-directory test as context authority. Selection corroboration and durable
  context corroboration are distinct questions and receive their original owners.
- `ReopenNative.expected`: delete flattening the original decoded identity into a
  string and rebuilding another identity object at the receiver.

These are unfinished working-tree deletions, not a completed global closure.
The remaining independent decisions are explicit blockers to Ready:

| Unchanged consumer | Independent answer still present | Required deletion |
|---|---|---|
| `historical_native_inputs.read_historical_native_inputs` | `root/native-sessions/recipient_lookup` selects admissible location | Recover exact original live-recorded context/source membership; corroborate its native identity without assigning selection authority to the path. |
| `continued_private_session.verify_continued_private_session` | parent-parent decides source class; parent name decides owner lookup | Derive original stable owner lookup from captured incarnation and whole original input/context coverage; preserve raw UNKNOWN. |
| `compaction_summaries.SelectedSummaries.reserve` | lexical private path decides fresh/continued coverage and raw refusal | Original enrolled fresh capability versus verified continued coverage owns the algorithm/case hook; storage path is not permission. |
| `attempt_recovery` | reconstructed private directory and only `--session-dir` prove native process absence | Observe original selected/admitted source with exact native/process/release proof; historical missing source remains unresolved rather than becoming NotSent. |
| `SelectedParticipant` / `run_selected` | native source publication not consumed from original AgentInfo | Reuse canonical `ThreadManagement.attach_session` publication with the captured owner/lease source transition; do not add a last-source cache. |
| tracked private launch | fixed private launch flags ignore ordinary configured settings/extensions | Complete the existing launch/configuration contract before claiming ordinary configured ACP continuation. No fixture-only override or disabled extensions acceptance. |
| paused next-model dispatch | active writer and summary commit still interpreted by idle assumptions | Extend existing custody behavior and delete idle reacquisition/retirement assumptions; preserve the same input and writer, never re-admit it. |

No partial factory patch is claimed to close these rows. Native input context
storage has a required all-or-none group; it cannot be partly populated as a
second source selector. Historical reservation rows without context remain
uncertain. Any required durable source relation changes must use the existing
input owner and Singer #495 preservation seam, not another codec/store/reset.

## Boundary ownership and final validation

- Arendt: complete selection/custody/admission/history/recovery/raw-coverage and
  current owner/tool sender consumers, phase/replay publication, paused lifecycle.
- Kepler #490: source membership, plural response proposal/publication producer.
- Sch #499: summary policy/packing and the existing current CoordinationSegment
  contributor across manual/adaptive/history/turn-prefix/map/synthesis.
- Singer #495: original native storage preservation; helper2 readonly findings.
- Parent: public installation/cutover. No public mutation here.

Source ownership and one coherent implementation precede validation. Final sanity
uses ordinary ACP on an original saved-state fork with the actual configured
provider/model/auth/settings/extensions: rename, channel and DM, compaction,
cancellation and distinct next input/terminal reply. It does not replay historical
UNKNOWN or substitute explicit loopback-selected files for ordinary continuation.
Supplied phase patch remains in #489; no separate TurnExposure/replay authority.
Patterns: IDEN-1/3/5/6, IMPL-4/5/13/14, BOUND-2, AGENT-2/6.
