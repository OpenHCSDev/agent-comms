# Real channel send and disclosure hot path

The user reported a long Enter-to-send pause in `#openhcs` and slow channel
thread-tree disclosure. One new human message was sent through the real native
UI, not a backend-only benchmark: `hey guys testing, please respond tersely if
you get this.` Canonical receipt is sequence 590. It was not replayed.

The real recording/profile is retained at
`/home/ts/.cache/agent-scratch/channel-send-live-profile-20261006`.
The original App completed and its captured processes were cleaned up; the
saved source owner was unchanged. Sender timestamps are construction times,
not commit clocks. Recorder phase durations and writer acknowledgments are
not input-to-photon latency measurements.

## Expensive work and changes

* `CliCommand.target_catalog` decoded the channel catalog for every command
  declaration. It now acquires one original `RegistrySnapshot` and
  `CatalogDocument` for that catalog operation and lends them to the existing
  polymorphic bindings. Execution still reacquires/rebinds current state.
  All channel/thread binding declarations were migrated, including channel
  Start/Stop membership and per-thread channel pins. No widget permission
  mirror or new cache was added.
* Human publication scanned the full strict wire again to check gaps and
  duplicate IDs after the durability owner had certified the complete source.
  That reconstructed private recipient policies and, especially, hundreds of
  silent retained-context manifests that cannot yield public messages.
  `CertifiedSourceRead.public_messages` now borrows its certified original
  prefix. `WireRecord` owns its public output: silent observations yield none;
  actual messages use the original public codec. Publisher retains the exact
  sequence-gap, reservation and duplicate-ID refusals before append.

The source certificate remains an acquired resource, not a new cache or
index. A reserved-but-unappended sequence can coexist with a valid committed
prefix; the read uses `require_open_prefix`, and Publisher still refuses that
reservation as an UNKNOWN human outcome. The initial stronger marker check
changed the refusal and was corrected; that negative is retained in the
original tool output. No uncertain message was replayed.

## Evidence and limits

Read-only comparisons use the real current registry and bus. Raw profiles and
small comparison scripts/results are under
`/home/ts/.cache/agent-scratch/channel-catalog-operation-20261006`.

* Seven `#openhcs` catalog actions are exactly equal. Catalog reads fell from
  51 to 1; one profiled operation took 250 ms before, 14 ms after.
* The final matched wire read borrowed the same canonical lock/prefix:
  all 599 `(sequence, sender, message ID)` answers were equal. Under cProfile,
  the original read took 13.085 s and the changed read 0.302 s. This is a
  profiled component comparison, not an Enter-to-paint timing claim.
* Nine existing declared/selected action controls passed in 1.13 s. Ten human
  ingress controls passed initially; the reservation/gap control exposed the
  marker distinction. After correction that exact control passed in 0.44 s.
* Existing Package parsed 324 production, 378 test and 54 tool modules with
  zero omissions. All four changed modules parse/compile; external direct
  binding-hook callers were absent. Dynamic third-party command extensions
  are not claimed verified.

## Installed result

The second actual native UI recording is retained at
`/home/ts/.cache/agent-scratch/channel-send-installed-check-20261006`.
It sent a different new human message once and exercised both disclosure
directions. Its source owner remained unchanged; capture and cleanup completed.
The message painted, but the UI still used about 84% of a core during the send
phase and about 92–94% during disclosure. **Whole send/disclosure latency is
not fixed by these component changes.** The remaining real stacks show
`viewer_snapshot -> display_view_metrics -> BusDisplayIndex.snapshot ->
DisplayMetricScope.observe_wire` decoding silent manifests, plus metric
projection encoding. That separate owner repair is with Heis.

The default `toad` link now selects the verified same-format client build,
including the sidebar session-update correction. The existing publisher gained
a concrete reviewed Core-client member: it changes the UI's imported supply,
not the running backend processes, commands, native package or active route.
All original class fields, bases and decorators in the four changed Core
modules match the running Core. All953 installed assets/full69 dependencies
match the selected Git/wheels. The final standalone channel resource correction
passed its two affected original controls; human reader/publisher bytes match
the physically exercised build. Publication is retained in
`.artifacts/sidebar-live-candidate-20261006/client-hotpath-final-wheel/publication.json`.
Both post-publication saved-history checks completed with the original source
owner unchanged and no remaining owned processes or cleanup errors. Native
module readback exposed my packaging mistake: copying the environment retained
the Toad interpreter trampoline. Putting the default directory first did not
fix it; the second recording disproved my initial PATH explanation. Both
recordings remain held as `channel-client-default-live-20261006` and
`channel-client-default-live02-20261006` under agent scratch.

The existing publisher now selects the exact physically exercised
`runtime-channel-hotpath` build (Core4cd81e96/Toadb1560a21), whose launcher names
its own interpreter. That recovery publication is retained in
`.artifacts/sidebar-live-candidate-20261006/client-hotpath-wheel/publication.json`.
The additional standalone-channel refinement stays in the branch for corrected
packaging; no PUBLIC prefix was edited in place. Neither capture establishes
smooth scrolling or improved whole-frame latency.

The actual submit stack exposes a larger synchronous obstruction before the
send worker starts: `CommsChatView.submit_input` disables its focused editor;
`Widget.watch_disabled -> blur -> Screen._reset_focus -> focus_chain` sorts
displayed children using `Widget._focus_sort_key -> virtual_region`.
Geometry lookup repeatedly enters `Compositor.reflow_visible -> _arrange_root`.
Between video21–34s,211 changed submit stacks include this call chain (210 at
the disabling watcher). These are compressed stack transitions, not samples
or duration estimates. The native focus/geometry repair is with Arendt; the
editor's send-custody protection must not be bypassed.

The screenshot identifies the independent compaction failure on
`openhcs-pr159-viewer-bind-owner` as `CompactionJournalError`. Mendel has that
actual thread and evidence. No failed compaction or user input was retried;
this client change claims no compaction repair.

## Combined sidebar read and focus successor

The sidebar display path now borrows the original certified opened wire cut.
The acquired descriptor and byte boundary remain owned by WireLog; strict
page/index readers keep their full decoder, while the certified public-message
projection skips silent records through WireRecord. DisplayMetricScope owns
its message projection, and AppendCheckpoint encodes its signed payload once.
Scope-only changes persist even when the original cut length is unchanged.
A durable reserved sequence does not invalidate the committed read cut; it
continues to block admission. The new real-store reservation control passed,
and the changed-cut/scope control passed. Initial environment and assertion
refusals are retained; no admission guard was weakened.

The actual native focus fix is merged. Screen acquires the geometry needed by
its original eligible focus chain once, instead of arranging the root for each
missing offscreen child. Existing focus ordering and overrides remain owners.
The combined candidate also includes the merged held-layout scroll repair and
sidebar session-update repair. All three distributions are reinstalled through
the original installer so each console command names its actual interpreter.
The full 953-asset/69-package installed source proof passed for this candidate.
The fresh actual send/tree recording is running; no whole latency result is
claimed before its terminal and profile are read.

### Combined real channel result

Recording `/home/ts/.cache/agent-scratch/channel-send-focus-installed-20261006`
sent one distinct new message through the actual native UI and exercised both
channel disclosure directions. Driver Return begins at video21.167s; retained
half-second frames show the new message by video21.5s and cleared input by22s.
This supports visible response within the first second, not a precise
input-to-photon percentile. The earlier recording retained the draft for many
seconds. The combined submit trace contains one changed focus stack, versus
210 disabling-watcher stacks in the old obstruction. These compressed stack
transitions are not call counts or timing measurements.

UI process CPU over the send interval fell from about84% to65%; collapse is
still about83% and expand61%. Tree/whole-frame latency remains unfinished.
The actual App exited0 and original cleanup retained no owned processes or
errors. The st wrapper exited1 with a terminal Input/output error after the
recorded App exit. The recording preserves this negative; no message or App
was replayed. Retained-only review completed and showed actual message paint
and tree changes. All runtime module readbacks select the combined interpreter
and packages. The original source agent stayed alive and unchanged. This
useful client successor is published through the existing atomic link owner;
backend processes and route remain unchanged.

Default-entrypoint confirmation completed at
`/home/ts/.cache/agent-scratch/channel-send-focus-default-live-20261006`:
the observed interpreter and modules select the combined client, saved history
and the existing scroll journey completed, App and terminal exited normally,
cleanup has no remaining owned processes/errors, and the original saved source
owner remains unchanged. This confirms installed/default selection and working
affected interaction; it does not establish smooth scrolling at all scales.
Core702 is merged; native83/84 and Toad507 are merged.

### Installed selection and parked sidebar checks

The existing private native App selection journey passed against the installed
combined client: thread/channel Ctrl toggle and Shift range, preserved right-click
selection/scroll, original dialog/read/archive execution, mixed command catalog
and truthful partial-failure notification. Output is retained under
`/home/ts/.cache/agent-scratch/parent-sidebar-selection-20261006/installed-selection.*.log`.
The user independently reports Ctrl-click working in the live installation.

The original parked-sidebar checks retain the same ContextExplorer, ContextTree,
nodes, source state and relationship rows across hidden preparation and A/B/A.
They pass using installed Toad/Core/Textual with the selected Python3.14, not a
source overlay. The initial Python3.11 collection refusal and stdin spawn error
are preserved. The fixed file runner passed both checks; its existing fixture
cleanup emits a resource-tracker warning, and a fresh private-use scan found no
remaining matching processes. This is native private-App retention evidence,
not a timing or large public context-tree performance claim. Logs and fixed runner
are under `/home/ts/.cache/agent-scratch/installed-sidebar-retention-20261006`.

### Disposable cleanup

Removed duplicated tracked-evidence directories from the completed frame/parking
wheel source archives (about340MiB); original repository evidence, wheels, proof,
raw recordings and journals remain. Cleared pip downloads and uv package caches
through their existing cache owners. The installed default still starts and
reports its version after cleanup. No source, private/public runtime or original
history was deleted. Remaining tree cost is assigned through the original
navigation and command-completion family; no second cache is being introduced.

## Navigation and hidden completion delivered

Toad PR508 is merged and the default `toad` now selects `runtime-navigation`.
This retains Core fefa72c and Textual be28b; it changes only Toad navigation and
completion supply. Already-running UIs retain their old imports until reopened.

The installed private App check (`navigation-installed-20261006/result.json`)
observed no service construction, registry decoding, action discovery or menu
row rebuild during idle observations. Opening completion still reads choices;
rename/deletion/new birth and unavailable-route behavior passed. The isolated
saved-history recording (`navigation-installed-physical-20261006/receipt.json`)
completed with UI/st exit0, original source unchanged, empty cleanup errors and
no remaining owned processes. No message was submitted. This is not a total
frame-time or channel-tree latency qualification.

The original 953 assets/69 packages and protected resources matched. The
publication is `.artifacts/sidebar-live-candidate-20261006/navigation-wheel/publication.json`.
A cache cleanup had removed the original Diff wheel. Rebuilding its exact Git
source reproduced SHA be7089c8 byte for byte; the original cache pathname was
restored and a durable copy now lives in `retained-artifacts/`. Preserve that
artifact for the current installed-proof/recovery relation. The initial verifier
refusal is retained under navigation-wheel/cache-path-refusal. Disposable copied
Git evidence was removed; original source/evidence/recordings are retained.

## Retained disclosure and prompt tab closure delivered

Toad509 is merged. The default client now selects
`.artifacts/sidebar-live-candidate-20261006/runtime-tree-close`; publication uses
the existing frontend publisher and changes only the Toad entrypoint. Backend
commands, processes, native package and route remain unchanged. Open clients
retain their existing imports until reopened.

Collapsed channel and relationship groups retain admitted row/prepared identities;
unchanged reopen no longer constructs rows or recaptures every member. Hidden
rows are excluded from navigation/ranges/animation; original publications still
retire stale membership, incarnation and route resources. The selected tab now
returns to its survivor before original teardown joins; cleanup remains awaited.
Channel participant output uses its original Static content and a single
presentation read for each member, with independent tooltip updates.

The combined installed private workspace check passed with empty stderr:
`/home/ts/.cache/agent-scratch/tab-close-20261006/installed.{stdout,stderr}.log`.
It preserves the surviving original editor, draft and Undo while removing the
closed admission/view. The initial source check emitted unregistered-thread
errors because its fixture lacked the canonical private declaration; the existing
fixture was corrected and the clean source/installed results retained separately.

Actual isolated-st saved-history disclosure recording completed normally under
`/home/ts/.cache/agent-scratch/tree-disclosure-installed-20261006`. Both native
clicks changed the tree, original source identity stayed unchanged, and cleanup
has no remaining owned processes/errors. No new message was submitted. Per-gesture
receipt timestamps bracket xdotool submission, not handler completion. Retained
10fps contact sheets show the changed tree near those submissions; no precise
input-to-photon claim is made. Initial expansion/collapse phase UI CPU was about
34%/27%, versus prior quiet33.5%/29.7%; this does not establish a reliable overall
performance gain. Previous baseline labels were reversed and remain preserved;
new labels are first/second disclosure rather than assumed direction.

All953 installed assets and69 package versions match their declared source/wheels.
The combined proof, activation and publication are in `tree-close-wheel`.
Activity-burst hangs, database read contention and the original pr159 compaction
failure remain unresolved by these changes. They are separate ongoing work.

## Native paint-only publication delivered

Toad510 and Textual85 are merged. The default client now selects
`runtime-paint`; its publication is `paint-wheel/publication.json` beneath the
existing candidate directory. Backend processes and native route are unchanged.

Static owns whether an update needs layout: native leaf Content with unchanged
text retains geometry while updating actual spans. Custom rendering/measurement,
containers and changed text retain layout. ChannelParticipants compares complete
Content identity, including spans, rather than plaintext equality.

The installed real producer check renamed a canonical participant without
changing its display title. The changed click target and tooltip painted, with
zero layout invalidations, requests or arrangements (577 widgets, ten tabs).
Its result is `participant-producer-installed-20261006/result.json` under agent
scratch. The initial verification had an unguarded multiprocessing entrypoint
and incorrectly assumed busy changes left plaintext unchanged; those failures
remain held. The corrected check uses the original managed rename owner.

The actual `paint-burst-installed-20261006` recording sent one distinct new
message and completed with the saved source unchanged and no cleanup errors.
Send-phase CPU was70.3% versus70.8% in the preceding burst: no meaningful overall
latency gain. Native writer completion intervals are not input-to-photon or
monitor frame times; captures can contribute gaps. Repeated coordination decoding
and layout remain in the profile. Existing accepted messages were not replayed.

Database attach failures are independently unresolved. The integrated runtime
diagnostic correction retains the original causal traceback and SQLite store
context before the unchanged wire error. It has not been installed into running
backend owners, and it does not identify the historical blocking writer.

Default-entrypoint confirmation completed in33.18s at
`paint-default-live-20261006`: observed modules/interpreter select runtime-paint,
both channel disclosure directions visibly changed the native tree, original
saved owner remained unchanged, App/st exited0 and cleanup has no remaining
owned processes/errors. No message was submitted in this confirmation. This
proves default selection and affected interaction, not smooth scrolling or a
whole activity latency win. The visible pr159 compaction error remains unresolved.

## Native tab-close observation and scalar decode delivery

The original SavedTabCloseJourney now resolves the captured native peer label and
its SessionTabClose through NativeFocusTarget. Seven guessed-coordinate options
and their duplicate screen-range validation are deleted. PhysicalJourney owns the
existing shared visible-history readiness command; warm and close consumers inherit
it. Review requires the peer admission to disappear, original selection/editor/
history identity to survive and reopening to create a fresh admission. Toad
checkpoint542d4a59 changes these two helper files only, with all other declarations
AST-equal except the shared readiness move and obsolete argument removal.

The first actual run is preserved under `tab-close-physical-20261006/run`. Its
unread badge cleared after capture, moving the close control five columns left.
Retained22.2–23.0s top-bar footage shows the pointer beyond the moved button at
the recorded22.493s submission; the tab remained open. This is a missed target,
not a measured slow close. No message was submitted. The second fresh App uses
the already-viewed peer and is retained under `read-settled` in that same scratch
root. Every native close/return/reopen check passed; footage shows the original
tab returning within roughly the first half-second of the gesture. This is one
observed settled case, not a latency percentile or proof against moving controls.
Both Apps/st exited0 and original cleanup completed without remaining processes.

Core704 is merged. FieldCodec retains representation precedence and the original
strict scalar validator, but exact scalar declarations reach it before unrelated
structural dispatch. Original real-registry measurement is25.09→20.62ms with equal
decoded documents; no new decoded-value cache or wire-format change. The reviewed
frontend wheel also contains the integrated same-format failure evidence changes.
They do not upgrade already-running backend owners or resolve the historical
SQLite blocker/compaction refusal.

The default client now selects `runtime-codec`; publication is the original
`codec-wheel/publication.json`. The953-asset/full69 proof passed. Actual isolated
saved-history scrolling completed in50.28s at `codec-scroll-installed-20261006`: 
PageUp, PageDown, reversal and End changed native reader/paint; End returned to
the tail. Observed modules select the candidate, original source owner remained
unchanged, App/st exited0 and cleanup has no remaining processes/errors. No new
message was submitted. This actual App confirms the affected installed read and
scroll path, not smooth scrolling or an overall activity latency improvement.

## Shared sidebar publication successor

Toad965c9504 integrates the complete sidebar/tab/unread consumer family from
Heis470671b79. CoordinationAccess now retains the original acquired publication;
SidebarSnapshot carries its captured row inputs, service, revision, worktree and
filters. Local admissions reproject routes without rereading the wire or resolving
each thread's presentation again. The application-level duplicate snapshot and
reset callback are deleted. Notification, transcript and context witnesses retain
their separate original lifetimes.

The private App at `hpub01/result.json` exercised eight consumers, three bursts,
hidden return, incarnation replacement, filters and service replacement. Each
distinct revision acquired one viewer snapshot and25 person inputs; reprojection
added no captures. A burst crossing expiry acquired two distinct revisions as
required. This verifies removal of duplicate acquisition, not installed latency.

Core05c35409 integrates original journal/recovery/annotation snapshot acquisition
followed by encoding and inspection outside the SQLite read. Multi-table atomic
cuts and refusal rules remain intact. This does not identify the historical UX
blocking writer or replace already-running backend owners.

The successor wheels and runtime-snapshot are prepared; the full953-asset/full69
source proof passed. The only Core wheel changes from runtime-codec are the three
reviewed read-lifetime owners. The installed channel-activity capture is using its
original recorder handle under `snapshot-burst-installed-20261006`; publication
and overall latency remain unproven until that result and actual review complete.
An initial recorder invocation supplied a prefix rather than the existing launcher
contract's bin directory and refused before output creation/App launch/input.
Correcting that operand did not repeat a submitted input or running App.

The original recorder completed once in60.63s. Actual source identity was unchanged,
App and st exited0, and cleanup retained no owned processes/errors. The new channel
message and responding agents painted; both channel-disclosure states were reviewed
from native screenshots. All observed modules select runtime-snapshot. There is no
overall latency gain established: send-phase UI CPU76.09% versus the prior70.35%,
with different uncontrolled live activity. Writer completion intervals had median
26.49ms/p95143.01ms/p99500.60ms/max3061.39ms; these are not monitor frames or
input-to-photon timings, and diagnostic capture can contribute gaps.

The existing reviewed frontend publisher selected runtime-snapshot as the default;
`snapshot-wheel/publication.json` is the original successful publication. Backend
owners and native package remain unchanged. The default link and version were
checked after publication. Tab labels and goal mentions still resolve original
ThreadView.presentation independently; Heis owns this remaining captured-presentation
consumer work. Shared acquisition is delivered, but the activity-hang problem,
historical attach blocker and pr159 compaction refusal remain unresolved.

## Captured tabs, sparse read progress and native dispatch successor

The integration now borrows the original sidebar publication for tab labels,
unread answers and goal mention titles. Native tabs validate the original wire
root and incarnation before borrowing the captured row; raw titles and marked
labels remain distinct answers. This deletes additional process-presentation
reads on the UI task.

Core display checkpoints now retain activity clocks independently of sparse
read progress and derive unread deltas from the existing certified page index.
Changed membership or alias inclusion still rebuilds the affected projection;
missing or stale page evidence falls back to the original bounded stream. No
new index, state store or weakened source decoder was added.

Textual PR86 is merged. Its existing message pump borrows each live class
mapping once per dispatch owner, constructs the private method name once and
filters message ancestry only when decorated handlers need it. Live replacement,
C3 delivery, selectors, instance binding and bubbling remain unchanged.

The selected three-wheel successor is `dispatch-wheel` / `runtime-dispatch`.
All953 assets, complete installed inventories and69 dependency versions match
the selected Git/wheels. Its actual saved-session disclosure recording is
`/home/ts/.cache/agent-scratch/dispatch-installed-20261006`; the original handle
is followed through completion. No human input is submitted in that run.
At this checkpoint the candidate has not been published as the default.

The dispatch successor's one installed saved-session disclosure check completed
exit0 in33.47 seconds. Both actual native disclosure clicks painted the expected
expanded/collapsed roster. UI and terminal exited0 after Ctrl+Q; original source
owner was unchanged; cleanup left no owned processes/errors. No message was
submitted. Default publication through the original reviewed frontend owner
completed in `dispatch-wheel/publication.json`; new UI launches select
`runtime-dispatch`, while existing open clients and backend workers are unchanged.

This is an affected behavior/delivery result, not a smoothness claim. The
read-only run's77 native writer intervals had median43.05ms and p95502.9ms;
stationary time and diagnostic captures are included. Those acknowledgments
are not monitor frame times or proof of a busy-agent latency improvement.

## Archived channel visibility after the reported removal

The user reported success when removing `#pr126-fixes`, but it remained visible.
A read of the original backend found that explicit tag still declared, no
members, and its channel preference archived. The exact original action is
not reconstructed; no removal or user input was retried.

The concrete owner defect was `ChannelView.roster`: show_archived filtered
thread members but never the channel itself. The shared roster now hides
archived channels unless that existing setting is enabled. Both channel_views
and CoordinationSnapshot consume this owner. Routing, catalog declarations,
history and the archive state stay canonical and unchanged. Toad names the
existing setting 'Show archived channels and threads?'.

The affected original metadata/routing control passes in0.46s and verifies
hidden normal roster, explicit archived visibility and original routing. The
first29-control channel batch had26 passes and3 failures: my added viewer
assertion used a store without a private protocol marker; two unrelated old
controls assume a native launcher and an obsolete Activity.readiness member.
I removed the out-of-scope viewer assertion from that storage fixture and reran
only the affected metadata control. Product admission was not weakened.
Existing Package parsed324 production,380 test and54 tool modules, omissions0;
the shared roster's two production callers were inspected.

The actual installed saved App finished normally in13.67s with unchanged source
owner and no owned cleanup remainder/errors. The installed current viewer
projection excludes `#pr126-fixes` normally and includes it with show_archived.
The tag and original archive state remain stored. Raw App/visibility evidence:
`/home/ts/.cache/agent-scratch/channel-archive-installed-20261006`.
The selected installed candidate is `channel-archive-wheel` /
`runtime-channel-archive`;953 assets/full69 packages match the selected source.
The independent equal-publication fanout correction is integrated at Toad
5e7a6d7f9 for the next affected App check; it is not in this visibility build.

## Equal roster publications: delivered successor

The original projection now retains newly acquired source custody without
rebuilding unchanged painted row answers. Pending actions still explicitly
reconcile, and incomplete/cancelled reconciliation invalidates the snapshot.
There is no new cache or revision mirror. Builtin channel lookup now borrows
Enum's own value map with an exact-value guard; original routing controls pass.

One actual installed saved App sent one new #openhcs message, opened the channel
and collapsed/expanded its roster, then exited normally in60.54s. The source
owner remained unchanged/alive; all owned cleanup completed. Raw evidence is
`/home/ts/.cache/agent-scratch/equal-roster-burst-installed-20261006`.
The send phase used54.86% process CPU, versus76.09% in the prior snapshot run.
Agent activity is uncontrolled, so this is not a matched speedup claim or proof
that all stalls are fixed. Visible sent-message and roster captures are held.

`equal-roster-wheel/publication.json` records delivery of runtime-equal-roster
through the existing frontend publication owner. New UI launches get this
build; existing open clients and backend workers were not restarted. Archived
channel visibility remains included.

## Transcript read lifetime successor

Mendel81f5beda integrated as da9504fb7. Native ancestry is acquired before the
coordinator read. NativeTranscript owns input selection; fragment reply queries
remain atomic, with one session header decode per fragment. All callers and
malformed-record dispatch inspected; three contributor private checks passed.

The actual installed saved-history App painted and exited0 in11.52s, no submitted
input, original source owner unchanged, clean teardown.953 assets/full69 match.
Evidence: `/home/ts/.cache/agent-scratch/transcript-lifetime-installed-20261006`.
Default publication: transcript-lifetime-wheel/publication.json. Initial publish
refused output-directory permissions before changing links; corrected the owned
directory to0700 and published without repeating the App. This removes proved
lock lifetime; historical database-busy cause remains unknown. Backend workers
were not restarted. An evidence append command was refused by the duplicate hook
because of its quotation parser; no runtime phase repeated.

## Actual right-sidebar acquisition profile

The original recorder now has a no-input sidebar_panels journey through existing
native target capture/click/wheel owners. One default installed App opened the
right sidebar, wheeled its viewport down/up, hid and restored it. It exited0 in
45.91s; original source owner stayed alive, cleanup had no owned remainder/error.
Evidence: `/home/ts/.cache/agent-scratch/right-sidebar-panels-installed-20261006`.
The same SessionThreadSidebar object140435932944704 retained99 widgets across
hide/return. Context records remained present and native context preparation
completed. This was a single-view hide/return, not a tab/eviction qualification.

Process CPU during open/wheel phases was97-101% of one core. The sampled original
call relation identifies ContextInspection.read -> WireLog.context_manifests ->
indexed_context_manifests -> original manifest decoding as substantial work.
It already selects this owner through ContextManifestSources; it is not decoding
unrelated manifests. Chrome transitions are not call counts or CPU attribution;
native DTO capture itself contributes observer cost. No input-to-pixel latency
or full smoothness claim follows. Parent reviewed actual right-sidebar footage
stills. Mendel owns scoped manifest/source acquisition; Heis separately owns
shared sidebar acquisition. Existing panels are retained, so adding another
panel cache would address the wrong source relationship.

## Shared channel participant acquisition delivered

ChannelConversation.update_roster now borrows CoordinationAccess.read_sidebar
with its original composer filters (stopped included, archived excluded). Both
participants and mentions derive from that acquired canonical snapshot instead
of acquiring coordination again for each channel page. Filter/currentness and
replacement semantics remain with CoordinationAccess; no additional cache.
Toad dadc805ead integrates the reviewed contributor change. The real private App
check held one acquisition across eight consumer requests and checked active-turn
filtering and replacement; evidence: `/home/ts/.cache/agent-scratch/hroster01`.

One actual installed saved-channel journey painted saved messages, expanded and
collapsed the channel tree, and exited cleanly in32.997s with zero submitted
inputs. Original source stayed unchanged; cleanup had no remaining owned process
or error. Evidence: `/home/ts/.cache/agent-scratch/channel-roster-installed-20261007`.
All953 assets/full69 matched. The original frontend publisher selected
runtime-channel-roster as the default, confirmed by the live launcher link;
publication is channel-roster-wheel/publication.json under the existing candidate
root. Existing clients and backend workers were not restarted. This confirms the
affected installed channel path, not a matched latency improvement or smooth
context-tree/burst behavior. Heis owns remaining row/history preparation;
Mendel owns scoped context acquisition.

## Certified context resources delivered

Core b7bfa90c5 and Toad4e5448603 retain decoded manifests in the existing
ContextInspection lifetime. Each acquisition still certifies the current source,
selects current incarnation pointers and captures their exact bytes; unchanged
source/pointer/bytes reuse the original decoded resource. Annotation, imported
provenance, SDK revision and model/settings reads remain independent. Writable
and archived WireAccess own their respective acquisition behavior; archives
retain strict original scanning. No widget cache or signature was added.
Contributor private-store checks covered unchanged reuse, new observations,
rename and tamper refusal. Parent inspected all state/acquisition consumers;
Package parsed288 source/406 tests/40 tools with no omissions and compiled the
changed inspection and original widget. External dynamic callers remain unresolved.

One installed saved-history App opened/wheeled/hid/restored the right sidebar,
exited0 in45.268s, preserved the original source and cleaned up all owned processes.
No input was submitted. Original recorded request/context resources remained
available. Evidence: `/home/ts/.cache/agent-scratch/context-resources-installed-20261007`.
Scroll-phase UI CPU was86.13/40.59/54.27/42.83 percent of one core versus the
preceding run98.45/97.39/101.33/96.80. This is an uncontrolled single-run comparison
with diagnostic capture overhead, not input-to-pixel/frame latency acceptance.
All953 assets/full69 matched; the original frontend publication selected
runtime-context-resources for new UI launches. Publication evidence is
context-resources-wheel/publication.json under the existing candidate root.
Original backend owners stayed running; no uncertain message was replayed.

## Worker diagnostics and recipient observation delivered

Core64f12229b moves MessageNotification's existing recipient selection ahead of
activity acquisition and projection. Sender/whole windows retain all outcomes;
strict assignment decoding/duplicates and transcript identity remain unchanged.
The real private mounted component preserved notifications and busy/idle while
reducing recipient activity observations8 to1; sender answers retained all8.
Evidence: `/home/ts/.cache/agent-scratch/observer-recipient-source-20261007`.

Native22e54a3aa (PR87) removes eager work-payload repr from the decorator and
WorkerManager. Worker owns the direct default; decorated diagnostics borrow the
callable declaration. Explicit descriptions, including empty strings, and actual
call arguments/lifetimes remain unchanged.61 original native controls and2 final
affected controls passed. The actual preceding profile showed retained inspection
repr on the UI thread through context presentation/contributor workers.

The installed panel attempt in `worker-declaration-installed-20261007` reached
open/down/up but its final CPython diagnostic request stayed unacknowledged;
the absent snapshot prevented hide/return. Original86299 exited1 and cleanup
retired its App; no input, original source unchanged, no remaining owned processes.
That full interaction remains unverified, with raw video/profile/manifests held.
An independent startup check first used an insufficient30-second recorder budget:
only1.25s remained for graceful exit, requiring owned retirement. That negative is
held in `worker-notifications-startup-installed-20261007`; no App timeout changed.
The correctly budgeted45-second no-input startup check passed and exited gracefully,
with saved messages painted and clean original custody/source. Evidence:
`/home/ts/.cache/agent-scratch/worker-notifications-startup-budgeted-installed-20261007`.
No broad latency or full sidebar-return claim follows.953 assets/full69 matched;
the original publisher selected runtime-worker-notifications for new UI launches.
Publication: worker-notifications-wheel/publication.json in the existing candidate
root. Native87 merged; original backend workers and uncertain inputs untouched.

## Relationship projections delivered

Toad9b526c640 projects relationship unread from the original acquired sidebar
publication. It no longer reacquires RouteSelection/service metadata for every
relationship row during native layout. The publication must still belong to the
observed service and match the bound root; navigation and writes retain their
original fresh admission checks. No unread cache or competing decision was added.
The contributor mounted component retained canonical unread across600 projections
and all group inputs with zero UI RouteSelection captures.

One actual installed saved-history App opened, scrolled, hid and restored the
right sidebar, exited0 in44.907s, preserved the original source owner and cleaned
up its owned processes. The same99-widget sidebar and original presentation
remained through hide/return. Scroll-phase UI CPU was30.55/32.80/29.39/34.89 percent
of one core. Earlier runs approached a full core; uncontrolled activity and
diagnostic capture prevent a matched latency claim. Native enqueue-to-writer
median2.07/p9513.62ms is writer completion, not pixels or input-to-photon timing.
Evidence: `/home/ts/.cache/agent-scratch/relationship-roster-installed-20261007`.
All953 assets/full69 matched. Original frontend publication completed and the
live launcher resolves to runtime-relationship-roster for new UI launches.
Existing clients/backend workers remain unchanged. Publication evidence is
relationship-roster-wheel/publication.json under the existing candidate root.

Removed about950MiB of redundant evidence copies from owned disposable Git build
archives, preserving original published source/evidence, wheels, proofs, runtimes
and raw runs. Burst activity, context-tree-specific scrolling, tab closing and
historical backend inbox failures remain distinct incomplete acceptance areas.

## Installed saved-tab close and return

One no-input installed saved-tab journey opened nra-architecture, clicked its
actual native close target, returned to openhcs-architecture-memory and reopened
the peer. All six original checks passed: peer opening, original selection after
close, original editor/history identity, retired closed tab and new peer admission.
Original run31515 exited0 in34.994s with clean owned process cleanup; source owners
were preserved. Parent inspected the actual close-done screenshot. Evidence:
`/home/ts/.cache/agent-scratch/saved-tab-close-installed-20261007`.
The close phase includes diagnostic exports and a deliberate settling wait; its
4.771s interval is not close latency. UI CPU averaged38.78 percent over that
interval. This proves the affected close/return behavior, not fast physical
response or burst activity. No message, provider request, source change or extra
publication occurred.
