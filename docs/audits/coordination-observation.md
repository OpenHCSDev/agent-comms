# Coordination observations: source ownership

Draft checkpoint. Installed acceptance and focused checks are not yet complete.

## Evidence and boundary

A bounded real codex/toad-theme page contained 16 claim notices and 6 semantic coordination messages; another codex/slop-census page contained 50 claim notices. Claims are required durable ownership records, not disposable chatter. The retained real UI frame capture (`slow-turns/sidebar-frame-1568605-frames.json`) also measured 57 `Prompt.sync_turn` binding refreshes in 15 seconds. It does not establish that all those refreshes came from claims.

The source chain is `Publisher.publish_claim_envelope` -> bus/store revisions -> `ObservationService.observe` -> `ThreadReading` -> `ThreadsObserved` -> Toad `CoordinationAccess.threads_observed` -> `Conversation.thread_observed` -> transcript publication and prompt synchronization.

Do not replace all physical bus changes with conversation visibility. `NotificationAssignment.source_paths` includes the bus, metadata and private checkpoint; silent context observations can change transcript coverage. Sidebar, notification and global revision behavior remains intact.

## Required answers

| State | Thread publication | Draft/page validity |
| --- | --- | --- |
| Claim/global-store change; identical thread, binding, notifications and transcript snapshot | No repeated presentation to an existing reader | Existing source checks still run |
| New native bytes, relevant receipt/content change, original turn/thread or binding change | Publish | Retain source, replacement and truncation fences |
| Busy, unreadable, absent or recovered thread | Publish the changed read state; keep busy reads pending | Never turn failure into unchanged success |
| New reader, even when thread-name membership is unchanged | Initial full publication | Reader obtains its own turn/read fence |
| Native append while reading a bounded page | Publish new history on the next observation | The native reader has no draft; valid append containment suffices for the captured page |
| CLI item finishes while reading its draft | Publish changed history | Reject mixing the newer draft state with the older finished-item cut |

`ThreadPresentation.same_observation` owns presentation equivalence. It preserves every presentation field, the exact original registered thread, and binding. `TranscriptReadIdentity.same_snapshot` already owns relevant page equality. `ThreadReading` retains one latest result instead of separate revision/retry facts; `ThreadsObserved` derives changed recipients. Toad consumes that result rather than deciding equality itself. New-reader interest resets this read lifetime through the existing interest request family.

The separate native attach defect was observed by core-route-counts without submitting input: “Transcript finished while reading its current draft”, while saved history rendered. `RecordsCut` now owns draft compatibility: `SessionCut` accepts valid append containment because native drafts belong to ACP; `ExternalCut` requires the same finished-item cut because completion retires its draft. `TranscriptReadIdentity` retains content/provenance checks and supplies the common validation to both draft read paths. Exact `same_snapshot` behavior is unchanged.

`SidebarResult`, the existing result ABC, owns the same revision-normalized comparison for every declared result. `SidebarReading` retains its last publication separately from read admission: roster/complete reads, expiry, unread-pending retries, and preparation completion all still execute before suppression. The entire snapshot, derived rows, request filters and read-marker notice participate. `UnreadableHistory` owns error equality through its original exception type/arguments; its duplicate runtime/reason attributes and duplicated argument comparisons are deleted. Failure, recovery and changed failures stay distinct. The measured `BusyRows` animation is independent and is not removed or attributed to claim appends.

Observation reuse belongs to `Observation.for_interest`; `ThreadReading` owns its irreducible fresh-reader lifetime. The intermediate `reuses_observation` behavioral flag and external request branch are deleted. An initial empty membership still publishes so the UI can clear old rows.

## Source census and new cases

Canonical refactor-audit `Package` parsing covered 320 Core and 281 Toad production modules, with zero parse omissions. The conservative AST query retained declarations, imports, bases through class sites, writes, reads and checks for the presentation/observation/cut family. Dynamic dispatch is not resolved by this query; the declared `RecordsCut` members and all production `ThreadsObserved` consumers were read directly.

Reproduce the decisive ownership searches:

```
rg -n 'class .*Cut|def draft_current|require_draft_snapshot|same_snapshot' src/agent_comms/{native_transcript,external_transcript,transcripts}.py
rg -n 'same_observation|changed_since|ThreadsObserved|for_interest' src/agent_comms/{thread_presentation,ui_model/observation,ui_model/status}.py
rg -n 'ThreadsObserved|result.changed|def show_thread' ../toad-first-run/src/toad/comms_root.py
```

A new record-source cut supplies its own draft contract in its declaration, with no caller enrollment. A new presentation field participates through dataclass equality, without another equality roster. The existing transcript owner determines page equivalence. No new index, cache, schema, polling loop, claim policy or input replay is introduced.
