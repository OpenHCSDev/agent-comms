# Saved history read state (core-batch-reply)

Base: Core `5d025464c`, Toad `6e05712f2`. Working checkouts:
`/home/ts/wt/core-batch-reply`, `/home/ts/wt/toad-history-read-state`.
Owned disposable tooling: `/home/ts/.cache/agent-scratch/core-batch-reply/venv`
and `skew-read/` (bounded AST/source evidence; remove after review).

## Owners and source evidence

- `ContextSegment` (`turn_context.py:331`) owns declaration discovery.
  `FieldCodec` decodes `type[ContextSegment]` through that family.
- `SegmentManifest.kind` (`turn_context.py:217`), nested contributors and
  `ContributionCoordinates.kind` (`turn_context.py:408`) store those names.
  They are durable identities, not just the roster of current prompt producers.
- `ObservationWireRecord` (`wire_record.py:144`) carries recorded manifests in
  silent bus rows. The crash was not a task attachment in a public message:
  the observation branch of `WireRecord.public_from_wire` decoded a silent row.
- `SegmentManifest` uses the type for public title, recorded part resolution and
  span membership. Replacing `selected_response` with `reply_route` on disk
  would change recorded identity and annotation membership. It is not an
  acceptable rename. #770 restores the actual stored declaration; the strict
  owning codec can read it without an alias or a second decoder.
- `ObservationService.observe` reads a global sidebar before individual threads.
  A sidebar decode error currently kills the observation process. Individual
  thread errors become only names in `ThreadsObserved.unavailable`, losing
  their explanation. `ReadRequest.answer` already contains an individual read,
  but sends a raw exception and Toad's transcript failure method raises it.
- Toad `CoordinationAccess.receive` treats observer death as an app exception;
  `TranscriptPublication.report_failure` also raises. These paths turn a
  persisted-data read failure into an app-wide failure.

## This checkpoint

Keep strict decoding and the actual stored member. At the existing observation
read boundary return a named unreadable-history state, preserving the original
reason and reader runtime. Sidebar and thread results carry that state; the
Core status model renders the reason. The paired Toad handlers and transcript
failure behavior are not implemented yet: the edit tool refused that checkout
because this thread is restricted to the Core project. Do not install this Core
checkpoint with the unmodified Toad result dispatcher. Coach has supplied the
normal remedy: `comms_set_project` to `/home/ts/wt/toad-history-read-state`, end
the turn, finish the pair there, then switch back. The declared CLI adapter is
`agent-comms invoke --tool comms_set_project --arguments '{"path":"..."}'`.
No shell source writes were used to bypass the original denial.
Genuine process death remains distinct from a read refusal. No unknown members
are skipped, guessed, converted on load or restored as compatibility aliases.

## Still required for full runtime agreement

The current `WireMetadata` and `ActiveRoute` store protocol/root/native-package
identity, not the Core writer build. An unknown name alone cannot establish
'newer writer': #755's deletion proves the inverse case (old record/new reader).
A writer/read runtime gate therefore needs a reviewed stored build/schema fact
at the existing store owner and a stopped install that carries existing records.
Do not manufacture writer versions from exception text.

#768's owner (core-route-counts) is editing `turn_context.py` and `wire_record.py`.
The shared-file handoff is on that PR; this checkpoint does not concurrently edit
those files or mutate the live bus. Any eventual removal of a stored declaration
must carry *all* manifests, nested contributors, source-span coordinates and
saved annotations through FieldCodec under the existing stopped-cutover owner,
with digests, text, sessions and UNKNOWN records preserved.

After review/merge I build a clean-export paired runtime; coach owns relink and
restart. Relink alone does not replace already imported code. The installer must
join outgoing writers/readers using the original process/route lifetime owners,
carry records while stopped, then restart them on the new pins. Never replay an
UNKNOWN attempt or change a blocked goal to make installation look successful.

## Verification of this Core checkpoint

- Reused refactor-audit `Package.load`/`overlay.py` for Core and Toad AST evidence.
  No parse omissions; lazy discovery and dynamic MRO targets still require the
  semantic source trace above (AST does not prove behavior).
- 37 focused checks pass: strict restored `selected_response` round-trip,
  unknown-name rejection, named error pickling/status, a real spawned observer
  surviving an unknown saved member, sidebar refusal followed by thread refusal
  and a successful independent read on the same pipe, observer-death separation
  and route checks. The follow-up checks cover unsupported-schema refusals on
  store and owner reads, unchanged contention/currentness exceptions, and
  sidebar deferral without replacing its original request. `AwaitedRead.refused`
  now owns the shared refusal boundary; `UnreadableHistory` declares the read
  error families once. The old service-local tuple and the store-only catch
  were deleted. `ObserveSidebar.answer` owns its read/result construction;
  scheduling and sent revision remain with `ObservationService`.
  No input was sent to a provider; the fixture bus is private
  disposable output under the owned scratch root.
- The configured installed interpreter, using this source, strictly decoded an
  actual 49,494-byte retained live observation containing a nested
  `selected_response` manifest (144 estimated tokens). Live bus bytes were only
  read, not changed. This checks a real saved record, not complete UI acceptance.
- Early fixture runs were invalid (inherited live N/K launch environment, then
  missing private marker/guard). The final fixture uses the actual protocol
  initializer; the command unsets live root/launch selections. Those initial
  failures are not attributed to the application implementation.
- Installed Toad journey and the writer/reader runtime gate remain unverified
  and unfinished. The branch is a draft checkpoint, not a delivered live fix.

## Retained installed loading diagnosis

Owner: core-batch-reply for Core/read-state and the paired refusal behavior.
Toad-ui-2/worker owns the separate empty-sidebar/history UI evidence; do not
start a competing ordering or Thinking-block implementation.

The installed screenshot was checked against its actual retained ACP log:
`/home/ts/.cache/agent-scratch/toad-links-596/state/toad/logs/agent-comms-acp_2026-10-13T13_18_13_203893.txt`.
It contains a nonempty `transcript_snapshot` before `session/load` finishes.
The installed controller (`toad/acp/agent_controller.py:143`) revalidates that
snapshot through `CoordinationTranscriptReader.publication` before publishing
to the view. The UI coverage summary alone cannot identify where it was lost.

Using the unmodified `toad-e55bedd7a-core-23f4699cf` interpreter with PYTHONPATH
unset and read-only Comms initialization: the retained snapshot strictly decoded
13 events, its currency read returned 18 events, and a fresh saved-history read
returned 18 events. An actual spawned installed observer, given sidebar interest
and a transcript request, returned 20 events in 1.56 seconds. These demonstrate
the working read path at those moments, not continuous mid-turn UI acceptance.
The first standalone spawn attempt used stdin and failed to import its main
file; that was a probe error, not an application finding. Closing the successful
probe before its pending sidebar send caused BrokenPipe during child retirement;
that is not evidence for the screenshot's initial failure. The owned helper and
completed fixture output are disposable and retired after recording these facts.

The coach's later concrete error is `Transcript read inputs changed during
preparation`. `TranscriptRead.read` (`transcripts.py:268`) checks its original
content identity before and after preparation. `TranscriptReadIdentity` includes
the entire native file revision while `page_bound` already captures the original
byte/receipt window. A native append during preparation can invalidate a bounded
page. The complete prefix/source relationship must be reasoned through before
changing it: do not ignore revisions or replace this with a bounded retry loop.
Read-currentness failures are now kept distinct from unreadable data, but that
classification is not a repair of the moving-source read.

`CursorPublication.observe` (`cursor_publication.py:117`) discards the error
behind unavailable verification. Its projection is independent of saved page
availability. Read-only SQL inspection found no current cursor for the observed
owner generation 1/admission 12282, and the installed native schema check passed.
The ACP load binding generation 10351 is a different domain, not that owner
generation. None of these observations proves the earlier discarded error.
Toad-ui-2 also demonstrated a saved page loading with same-runtime absolute ACP
and cleaned launch environment while verification stayed unavailable; absence
of a live `AgentThought` is not absence of saved history.

The last pre-restart tool request became UNKNOWN during read-only source queries
and status inspection. It was not retried; no input, install, project change or
provider request was in that command. Original sessions and all uncertain input
records remain untouched.

## Core DM feedback contract findings (unimplemented)

Core-batch-reply owns the missing Core contract; Toad-ui-2 owns the shared drawn
reader. `MessageNotification` exposes frozen `recipient_identity`, handling
`state`/`detail` and human `displayed_to`; there is no per-message native-input
reading field. CLI `Checked by CLI` (`agent_activity.py:89`) comes from exact
incarnation/inbox acknowledgement, not completion or model comprehension.
Native proof must come from the existing `NativeRuntimeInput` source membership
and committed journal/input proof, never from Responding/Responded labels.

Human reply feedback has two separate source gaps:
- `ResponseConversation.audience` (`response_conversation.py:58`) appends an
  original sender only when executable. With a human-to-agent DM whose sole
  frozen recipient is the responder, removing that responder leaves an empty
  reply audience. `NotificationAssignment.for_deliveries` projects only that
  stored audience; no human result or `displayed_to` can be guaranteed. This is
  a source finding; the bounded retained toad-ui-2 DM window contained no matching
  reply, so no live reproducer is claimed. Repair must preserve the human as a
  recipient without scheduling an executable human or rewriting saved records
  through a second codec.
- Native paint calls `HistoryViews.mark_thread_view_read` (`history_views.py:399`),
  then `TranscriptReadState.mark_read`/`ReadLedger.mark_transcript`. It records a
  native offset, not `ReadLedger.messages`, which `displayed_to` reads. Explicit
  human DM pages already use `DMDisplayBasis.acknowledge` (`read_basis.py:218`)
  to record exact painted message sequences. Native pages need the corresponding
  Core-owned original-message display basis and mark operation; a transcript
  offset, page fetch or handled state cannot supply that evidence.

Acceptance for the eventual Core change: canonical human-to-agent input and
agent-to-human response retain exact identities; accepting an assignment alone
never reports native input reading; committed input proof does; physical native
DM paint marks only the actually drawn original message references; ReadNotifications
returns the human result/display mark after that operation. Preserve older unread,
filtered and replaced-source cases. No such production change has been made here.
