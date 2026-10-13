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
existing UI models and transcript owner show it instead of crashing the app.
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
