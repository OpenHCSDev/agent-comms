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

The refreshed working-source census has 35 requested Python root declarations,
71 declarations including inherited implementations, 421 methods, 2,488 lexical
same-name call candidates and 2,353 projected flow candidates. NRA resolves 546
exact function edges; the remaining 1,807 are retained with their possible
symbols/open resolution.
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

The authoritative paper is Zenodo record 22782549, published 2026-09-29:
main sections 6.2/6.5 and Corollary 6.3, supplementary sections 4.6/4.8.
The original downloaded main/supplementary PDFs and checksums are held in
`/home/ts/.cache/agent-scratch/oopsla-zenodo-latest-20261001/`.
Batching reduces operations, not independently supplied semantic answers.
Native inheritance and overlapping capability MRO recover the determining
declaration; generated external routing does not establish source recovery.

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
- `PrivateSendAdmission`: delete stored bus/store/root/owner/participant copies;
  the same acquired `SelectedParticipant` supplies those facts at the boundary.
- `NativeRuntimeInput`: bind the original emitted `NativeSessionIdentity` with
  the send epoch before bytes. Commit only the later context receipt against
  that selected source. Delete post-result source selection as the source of
  recovery identity. No new column, proof store or reconstructed old UNKNOWN.
- `NativeContextReference`: recorded/unrecorded completion belongs to this
  existing family. Admission and tool eligibility consume `.recorded`; historical
  and continued receipt acquisition calls `NativeRuntimeInput.require_context_proof`.
  Delete those readers' independent context-construction algorithms. SQL NULL
  classification stays at `NativeInputContext.reference`, not its consumers.
- `Registration.attach_native_session`: source publication checks the original
  `RegistryOwner` against the locked determining document and uses existing
  registration transitions. It retains the current phase rather than comparing
  an obsolete full Thread. Selected attestation and ordinary `TurnProgress`
  consume this same transaction and its returned original owner snapshot.
  Explicit operator `attach-session` is a different authority: its exact captured
  Thread CAS can also replace a process. It does not grant native admission.

These are unfinished working-tree deletions, not a completed global closure.
The remaining independent decisions are explicit blockers to Ready:

| Unchanged consumer | Independent answer still present | Required deletion |
|---|---|---|
| `continued_private_session.verify_continued_private_session` | Directory/routing-name selection deleted in the receiving coverage increment below | Original `NativeRuntimeInput` selected session plus committed reference determines receipt membership. A current routing name cannot erase the original source's history. |
| `compaction_summaries.SelectedSummaries.reserve` | Caller-side fresh/continued coverage selection deleted in the receiving coverage increment below | Existing `PrivateInputs` consumes original enrollment/raw receipts; the allocated storage namespace supplies only a prewrite-marker obligation. |
| tracked private launch | fixed private launch flags ignore ordinary configured settings/extensions | Complete the existing launch/configuration contract before claiming ordinary configured ACP continuation. No fixture-only override or disabled extensions acceptance. |
| paused next-model dispatch | active writer and summary commit still interpreted by idle assumptions | Extend existing custody behavior and delete idle reacquisition/retirement assumptions; preserve the same input and writer, never re-admit it. |

Historical reads now recover the exact original row's identity and context;
recovery observes both original `--session` and launch-resource `--session-dir`
process forms. A directory cannot select the saved source. Historical rows which
never recorded a selected source still refuse recovery inference.

No partial factory patch is claimed to close the remaining rows. The original
source pair and later context receipt are different acquisition phases of the
same SQL row. NativeInputContext's all-or-none group describes the context receipt;
the context anchor no longer mistakes prewrite selected identity for completion.
Native schema 5 to 6 is an unfinished declaration transition, not a deployed
format or permission to reset original rows. Singer #495 owns the eventual
frozen one-shot preservation seam. Existing complete contexts and NULL historical
UNKNOWN must remain byte/field identical; no carry has been executed.

## Shared batching ownership and consumer census

Kepler #490 is the sole writer of the COMPLETE plural builder: ExecutionStore.create,
WakeAssignment/ExecutionAssignmentLink, RecoverySnapshot obligations/intent/receipts,
response preparation/publication/terminal/recovery, SelectedAttempt.engage/run,
SelectedParticipant.select and CoordinatedTurn.capture. This receiving scope
supersedes the previous partial handoff. Arendt does not edit those scalar
algorithms in parallel. Our protected shared selected_turn change only passes
the original returned context to `session.continued`; private_send_stage recovers
the original recorded identity instead of rebuilding a private parent.

The determining relation is `create(sources=batch.sources)` to original ordered
membership and route obligations, then `require_wire_responses()`. ALL original
route bodies are prepared before ANY append; each publication consumes its exact
original obligation and owner witness. `require_published_evidence(route)` returns
the original intent/receipt and terminal carries the receipt tuple. There is no
first-route subset, scalar compatibility alias, copied message ID/target set or
parallel response authority.

| Consumer boundary | Required original relation and deletion |
|---|---|
| producer / normal CLI / foreground | Existing execution owner captures every original source and target; delete scalar execution construction and imports. |
| native / tool | Shared selected algorithm prepares and publishes every route; tool capabilities retain per-original membership/audience, not the first assignment as batch authority. |
| historical | Membership joins original ExecutionAssignmentLink or preexecution TriageNativeSources; receipt/context storage is not a second membership decision. |
| recovery | RecoverySnapshot receives complete obligations; existing ResponseState and PublicationIntents own receipt presence/identity and envelope validation, respectively. Delete copied checks. |
| terminal receipts | Original publication receipt tuple, never reconstructed IDs/targets or scalar result fallbacks. |
| cursor / coverage | SourceCoverage proves the entire original frozen prefix; CursorOwner admits monotonic publication. Batch size or route order cannot replace per-original handling evidence. |

The NRA lexical/exact/open census is source evidence, not a declaration that
Kepler's scalar closure has already landed here. His exact implementation/deleted
site census must reach both PR bodies before parent review. Native6 and broader
saved custody work do not hold that independently coherent batching checkpoint.

### Normal builder integration

`d15362ba` publishes this scope's source implementation and refreshed NRA
fingerprints; 194 production lines deleted / 324 added in 25 files. It is not
Ready. The native source/schema transition and configured custody obligations
above remain unfinished. No tests or native inputs ran in this source pass.

`146388dc` normally merges Kepler's full plural builder `369b10be`, preserving
both branches. The only explicit conflict was `AttemptStore._resume_retry`:
existing `ReplayAssessments.revoke_for_retry` retains the replay decision,
while every original obligation resumes through the plural exact-target update.
The automatically joined UNKNOWN path similarly retains `ReplayAssessments.accumulate`
and the complete publication-intent tuple. No copied replay flags are restored.

Source search after integration has no production `snapshot.obligation`,
`snapshot.publication_intent`, `execution.exact_target` or `require_response_target`
consumer. Shared selected continuation still receives `result.context`; original
native source verification and observed-attestation publication survive the merge.
Kepler's `490-source-fidelity-20261001.md` supplies the complete builder census.
The NRA artifact here fingerprints the **premerge d15362ba source**, not the
expanded integration; its exact/open edges are not silently relabeled current.
Configured multi-route and saved-custody validation remain final obligations,
not claims supplied by these searches.

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

## Original configured compaction publication relation (#501)

The independently merged #501 (`b4ea9f8f`) classifies the existing
CompactionSourceProgress leaf phase as an observation, retaining raw stream
semantics while excluding leaf changes from aggregate turn equality. Its
actual configured saved-fork snapshot has 1,042 raw observations / 363 leaf
switches / zero aggregate work changes and one whole-turn publication. This
is publication evidence, not a complete UI or controlled latency acceptance.

The same single original input later reached native commit `1a251fa0`, user
`1dd3b365` and assistant `4332fd6f` with stopReason=stop. The original typed
receipt linked operation `bc86b8f7f27f452da0e72a3019d71fd3`, committed its exact
native entry, recorded the original input started/unresolved=false and observed
no active turn. The UI had already failed on tab return with source-reader
retirement. Source and terminal evidence live on #501 at `540b198b`; no replay,
provider invocation or lifecycle mutation was performed by the observer.

Source-consumption counters and their native observation clock are owned by
CompactionPlan; they do not carry a provider-terminal timestamp. The original
851.234-second interval is last source observation to commit, not known provider
completion to commit. The fresh single UI submission to persisted answer was
132.327464 seconds; these different attempts do not isolate any stage's cost.

The complete downstream seam remains in this scope: SelectedPiSummaryRpc awaits
the original on_event callback; AcpEventConsumer observes canonical compaction
before publishing the original raw update; TurnRunner.observe_compaction reads
the original bound lease/phase and transition_turn rereads phase for equality
before AgentActivity/Registration/RegistryDocument publication. These are actual
consumers/questions, not permission to introduce a cached phase, event coalescer,
timer or independent proof. Their per-delta cost has no retained timing bracket
yet. Native custody/receive ownership and exact lease publication must be closed
together; the presence of two reads alone does not justify a symptom patch.

## Next code-bearing launch/source checkpoint

Main `3c6df83a` (#490/#501) is normally merged, preserving the unfinished
Native6 declarations and original UNKNOWN references. This increment changes
seven production files: 130 lines added / 163 deleted, no new type or store.

| Removed decision/copy | Existing owner and complete production callers |
|---|---|
| CLI, managed and tracked each build Node bootstrap, import fence, module environment and configuration normalization | `NativePiRpcLaunch._build` owns one shared acquired-process construction algorithm. CLI `main`, `managed`, and `tracked` all consume it after their original package validation. |
| Tracked ordinary continuation unconditionally disables automatic extensions/skills/context files | Existing `SelectedSession` ordinary behavior retains configured discovery. Existing `FirstSelectedSession` alone owns its original offline first-start isolation, exact creation receipt/header and tool denial. No caller switches on a copied selected level. |
| `NativePiRpcLaunch.private_environment` chooses an isolation branch from separately supplied selected level | Deleted. `SelectedSession.launch_environment` and inherited ordinary saved behavior preserve configured credentials before installing private no-retry settings. `FirstSelectedSession` supplies its existing distinct isolated environment. |
| `TrackedTurnSession.execute` accepts a separate fresh receipt and re-derives its level | Deleted parameter. Launch and `NativeStartupAdmission` receive the SAME original selected session; startup derives from `session.startup()`. `PrivateSendAdmission` no longer separately passes the receipt. |
| TurnRunner owns a native identity environment unavailable to selected participants | `Thread.native_environment` projects original registry fields through existing `ProjectRuntimeRequest.for_native` full owner/process binding. `OwnedTurn`, preparation and `PrivateSendAdmission` all receive that projection; old `TurnRunner.native_environment` definition is deleted. |

The pinned native CLI source confirms `--no-tools` means all tools, including
extension tools (`main.js` passes `noTools="all"`; SDK allowlist becomes empty).
Configured discovery therefore does not turn triage's original no-tool contract
into an instruction-only promise. Coding tool policy, prompt admission, package
trust, first-start mint and UNKNOWN settlement remain their existing owners.

This is a source checkpoint, **not Ready**. No sanity or native input has run
during reasoning/implementation. Remaining constructors include obsolete test
callers passing session_dir/session_file or fresh_selected instead of original
SelectedSession members, and the project-observation fixture's deleted runner
method. Those must migrate before final validation, without compatibility aliases.
That checkpoint still inferred continued lookup from a directory and classified
coverage in reservation; the receiving coverage increment below deletes those
decisions. Paused dispatch/recovery/source-switch custody
still require complete closure. Native6 preservation/carry and actual configured
ordinary saved/fork/cancel/continue validation remain final obligations; Native5
receiving release is independent. The prior NRA artifact is not relabeled to
fingerprint this newer source.

## Receiving coverage implementation after current-main integration

Current main `b2834902` (#495/#500 included) is normally merged at `7bf62729`.
The two merge resolutions retain main's nominal `NativePreparationResult`
return/compaction behavior together with this scope's `FileRevision`, and retain
both native context hooks (`requiresCompaction`, `summaryDeclineReason`). There
is no rebase, lost parent contribution or Native5-to-Native6 acceptance inference.

The subsequent code increment changes six production files, deleting 87 lines
and adding 100. It adds no class, schema, store, alias or proof source.

| Deleted independent answer | Existing determining owner and consumers |
|---|---|
| `session.parent.name` chooses native SQL membership; current `owner_thread` filters committed source history | `NativeRuntimeInput.recorded_contexts` queries the original admitted session file and requires the original `NativeSessionIdentity` from its header. Every selected row must supply its committed `NativeContextReference`. `continued_private_session` is the consumer; it cannot mint a receipt or accept an incomplete prewrite context. |
| Continued coverage requires a specific parent-parent directory | Deleted. Original native identity, exact reserved revision, original started-input text/digest, committed context and retained-history proof cover the selected source. Directory ancestry grants no history or owner identity. |
| Fresh coverage reconstructs stable lookup from `fresh.path.parent.name` | Existing `FreshCoverageIdentity.require_owner` checks the original witness incarnation/process. `PrivateInputs.require_coverage` still requires the exact process-local returned enrollment ACK. Its stored stable lookup remains the original allocation fact; no consumer manufactures it from a path. |
| Reservation separately interprets fresh/continued/raw cases | `PrivateInputs.require_source_coverage` owns the algorithm on the existing journal owner. `SelectedSummaries.reserve` consumes it under the same wire/input/journal custody. A fresh mint must have its exact enrollment and no raw UNKNOWN; continued raw IDs require complete original context corroboration. |
| Reservation and send fencing separately derive the private writer namespace | `PrivateInputs.requires_raw_marker` declares that physical allocation obligation once. `send_fence` and source coverage consume it. It grants neither source selection nor enrollment; recorded raw/enrollment obligations also apply outside the namespace. |
| Coverage directly compares a stored input's raw owner-name field | Existing `StoredInput.matches_owner` supplies that existing storage relation. Historical rows still do not attest birth time; live full-incarnation selection is independently retained by the original `SelectedSource` owner witness. No global incarnation-equality relaxation or name alias is introduced. |

This is code-bearing **unfinished source**, not Ready. The previous NRA census
remains labeled with its original fingerprint. No tests, native inputs, installed
runtime changes, public mutation or historical replay ran during implementation.

Remaining concrete closure obligations:

1. Remove independently supplied launch/turn source fields and close ALL normal,
   tracked and preparation constructors through the original `SelectedSession`.
   Preserve native-generated and explicit fork behavior rather than guessing a
   default saved identity from a launch directory.
2. Existing `NativeCustody`/`TurnSession` must own paused-dispatch receive and
   commit through the active writer. `SelectedSummarySlot` still assumes an idle
   retained child, and `compact_owner_once` still retires it for external commit.
   These are not repaired by the coverage change or by pending-query correlation.
3. Migrate obsolete constructor consumers, then close original Native6 storage
   preservation with Singer. Native5 carry qualification cannot supply this.
4. Only after the coherent source closure, batch affected sanity and run the
   actual configured ordinary saved-fork/channel/DM/compaction/cancel/continuation
   journey. No old input or uncertain provider operation is replayed.

## Receiving constructor and reopen implementation

The next source increment removes the independently supplied selected-file and
directory fields from `NativePiRpcLaunch`, the duplicate selection from
`TrackedTurnSession`, and selected-file/fork fields from `TurnSession`. A launch
retains the ORIGINAL `SelectedSession` object, not a second identity or reader.
`PersistentPiSession.open`, tool-resource acquisition, startup scheduling,
ordinary `stream_agent_events`, tracked input, and `NativeSessionPreparation`
consume that object. Saved managed launch now uses the same original native
reopen helper and expected-ID attestation as selected saved continuation. A
generated session still receives identity from native, not a guessed file name.

The existing `SelectedSession` leaves supply pending attestation and startup
behavior. `FirstSelectedSession` alone supplies the original minted startup
capability; the separate `fresh_selected`, file equality reconstruction and dead
`startup()` projection are deleted. The public native CLI consumes the shared
bootstrap algorithm directly; it no longer constructs an RPC object with an
invented unselected-session field just to execute Node.

Reopen custody now retains ONE original `NativeSessionIdentity`, deleting its
separate file and nullable session-ID fields. Retained/borrowed states consume
the original observed identity. Every production poison/external-write caller
passes that original value: selected policy observation, selected summary RPC,
and `compact_owner_once`. Managed selection revalidates the original source via
the existing native reopen helper, and the successor compares that selection
against its original identity before attestation; it does not repeat source
selection by parsing the same file again.

Source-selection CLI options are declared on the existing `NativeArgument`
family. Their shared capability rejects a second configured session selector in
managed RPC; the native CLI still owns those external options unchanged. This
closes `--session`, `--session-id`, `--session-dir`, `--fork`, `--continue/-c`,
`--resume/-r` and `--no-session`, rather than treating them as unrelated raw
tokens that can overwrite the captured owner selection. The unused managed
`fork_session` flag chain is deleted; production caller search found no requester.
Ordinary ACP fork remains `ForkSessionHelper` → original `NativeSessionIdentity`
→ registered child source, not that alternate launch flag. The generic native
CLI's original fork option is retained.

Two unused context hook definitions (`summaryDeclineReason`, `requiresCompaction`)
are deleted after searching the Python, native builder and fixture sources;
there is no dormant paused policy asserted by their presence.

Remaining source obligations are NOT hidden by these deletions: paused
next-model dispatch still needs its original active writer/receive/commit owner;
the tracked transport acquisition error boundary precedes its native failure
handler; obsolete fixture constructors must migrate; Native6 preservation and
the final configured ordinary workflow remain unfinished. In particular Singer's
new socket-start failure is before native spawn, but historical NULL UNKNOWN is
not automatically reclassified using that fact. No native/provider input, tests,
public restart or installed runtime mutation occurred in this implementation.
