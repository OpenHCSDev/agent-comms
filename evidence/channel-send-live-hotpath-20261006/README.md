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
