# A2 codec ownership closure

Base: parent `fix/compaction-failure-recovery-20260928` at `7c51d789`.
Own tree: `/home/ts/wt/comms-field-codec-closure-20260928`.
Branch: `fix/field-codec-closure-20260928`.

Removed `MessageWireCodec`, inherited `TranscriptCodec`, the transcript-only
`TranscriptRoutingStorage`, both handwritten claim wire functions and the
fixture-only complete-line parser. All callers now use `FieldCodec` directly.
No compatibility names, re-exports or replacement codec introduced.

The shared codec encodes declaration-owned `projected(view="wire")` properties
and ignores those derived fields on input; `Message.message_id` owns durable
`id`. A shared `wire_nonnull` field constraint preserves absent-versus-null
admission semantics. Existing `wire_required`/`wire_omit_default` metadata now
owns the claim shape. No scalar wrapper is needed for ordinary dataclasses.
The duplicate parser's 16 KB claim-only cap and publisher serialize/parse probe
are deleted; actual bus validation remains authoritative. The large-claim test
now proves >16 KB publication, reopen, projection and export work end to end.

The permanent AST guard scans all production source, follows aliases, module
references, imported/re-exported bases and inheritance, and has no exemptions
outside the codec's owning module. Its own test distinguishes unrelated classes
with the same short name. Parent separately owns SelectedSourceCodec removal.

Production: **48 lines added, 151 deleted**. Tests: **192 added, 78 deleted**;
the increase is the required permanent alias/inheritance-aware AST guard.

## Evidence

- `focused-final.log`: 66 passed in 3.51 s. Codec, pure claims, native transcript
  parsing, tool diffs, routing persistence, selected resource admission and guard.
- `installed-path.log`: 7 passed in 0.78 s against a built/reinstalled wheel,
  verified import from this tree's `.venv/lib/python3.14/site-packages/agent_comms`.
  Actual isolated bus claim/release, durable external shape, large-claim export,
  selected admission and SQLite transcript routing/reopen; no bus/storage mock.
- `guard.log`: 2 passed. No production FieldCodec descendants outside its owner.
- Initial `focused.log`: 64 passed, 9 failed. One changed test still expected the
  deleted parser's exception class; corrected to require shared decode ValueError
  and the same specific invalid-version reason, then included in the 66-pass run.
- `baseline.log`: unchanged parent source/tests independently reproduce the other
  eight failures (18 passed). Existing private-checkpoint fault-injection tests
  expect earlier failure text/append timing. The unchanged failures are:
  malformed_complete_row; durable_raw_alias_claim; failed_fsync[bus];
  fsynced_existing_bus_row_marker_loss; invalid_claim_record;
  claim_bearing_failed_fsync; claim_bearing_no_newline; sigkill[after_append].
  These are reported to the parent; this change does not claim that suite green.
- `bus-roundtrip.log`: first selected command had a nonexistent test selector,
  executed zero tests. Corrected selector used in the passing installed run.
- `build.log` / `install.log`: wheel build and noneditable install receipts.

No live roots, identities, rules, launchers or parent files modified. Parent's
uncommitted work remains untouched. CI deferred. No remaining diagnosed codec
source/caller blocker; parent integrates this commit into PR272.
