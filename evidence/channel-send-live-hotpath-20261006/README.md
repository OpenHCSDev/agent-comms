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
