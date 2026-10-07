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

These Core changes are implemented and checked, **not installed/live yet**.
The current frontend-only publisher deliberately requires unchanged imported
Core bytes. Delivery must use the actual reviewed publication/recovery owner;
do not silently relax that check or edit a PUBLIC installation. The earlier
real UI profile is the baseline; an affected installed UI check is still due.
The independently reported compaction error is not identified or diagnosed
by these findings.
