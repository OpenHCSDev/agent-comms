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
The first post-publication saved-history check completed with its original
source owner unchanged and cleanup reporting no remaining owned processes or
errors. Native module readback showed that inherited PATH selected the prior
candidate rather than the newly published default; it cannot establish the
final default's live behavior. That original recording is preserved at
`/home/ts/.cache/agent-scratch/channel-client-default-live-20261006`.
The corrected default-entrypoint check puts the actual default directory first
and is recorded separately. Neither capture is a smooth-scrolling or frame-time
improvement claim.

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
