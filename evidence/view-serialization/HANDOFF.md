# Declared view/filter boundary completion

Base: core main `3345618` (PR170). Branch:
`codex/refactor-view-serialization-20260928` in
`/home/ts/wt/comms-refactor-view-serialization-20260928`.

## Implementation and deletion closure

- `declarations.py`: ViewMatch is now the existing DeclaredFamily mechanism with
  an abstract matching contract. AnyOfMatch and AllOfMatch own their relations.
  ViewPredicate stores `type[ViewMatch]`; FieldCodec decodes the external name
  once. Removed enum constants and the recovered-case matching switch.
- Deleted ViewPredicate.to_wire/from_wire, SavedView.to_wire/from_wire,
  SavedView.matches, and both constructor re-decoding paths. No aliases or
  old-interface constructors remain. Predicate matching binds its owned tags
  to the selected relation; consumers call `view.predicate.matches(tags)`.
- SavedView declares that its name is excluded from the catalog row, because
  the enclosing catalog key owns it. Existing FieldCodec.project derives the
  row and FieldCodec.encode derives complete adapter output. No codec fork,
  registry, store, cache or new storage schema was introduced.
- `channels.py`: consumes FieldCodec directly, removing the hand-maintained
  serialized-field exclusion. Its real saved-file boundary supplies the key
  and preserves genuinely missing historical dates as zero. Duplicate row
  names are rejected rather than silently overwriting a second authority.
- `tools.py`: directly encodes declared views and decodes nominal match types;
  tool match choices derive from ViewMatch.names(). All current core tests and
  constructors use AnyOfMatch/AllOfMatch. `__init__.py` exports those real owners.

ViewKind remains a passive participants/activity value: current source has no
case-dependent behavior or dispatch for it. Its existing JSON spelling and
FieldCodec handling are unchanged. ThreadStatus and Pascal's lifecycle work
are untouched; declarations.py changes are confined to imports and the view
section. No Toad ViewPredicate/SavedView/ViewMatch consumer was found in the
current parent Toad source/tests; no paired UI source migration is required.

## Current contracts / saved data

Internal construction:
`SavedView("work", ViewKind.ACTIVITY, ViewPredicate(AllOfMatch, frozenset({"api", "ui"})))`.
Serialization: `FieldCodec.encode(view)`, `FieldCodec.decode(SavedView, payload)`.
The existing complete JSON remains:
`{"name":"work","kind":"activity","predicate":{"match":"all_of","tags":["api","ui"]},"created_at":123.5}`.
The existing `saved_views.json` remains `{"views":{"work": <same fields except name>}}`.
No destructive migration or alternate reader is needed. Reads do not rewrite
saved data; known timestamps survive write/reopen and absent timestamps stay
unknown. Current store owner, locking, registration and runtime policies remain
in place. No live root was written and no live worker was restarted.

## Local evidence

- `verified-tests.txt`: **139 passed**, 2.59 seconds, xdist/coverage default
  addopts disabled. Includes channels, saved-file boundaries, S4 ownership,
  channel display, exports, FieldCodec, tools, DeclaredFamily and historical views.
- `cli-acceptance.json`: **7 fresh CLI processes, all exit 0**. Actual shared
  invoke tools create tags/view, reopen/list, rename tags, delete/list. Typed
  all-of behavior and stored timestamps verified after reopen; no bus messages.
  Script: `cli_acceptance.py`, fixture under owned persistent worktree and
  removed on exit. This is source-runtime local acceptance, not installation.
- Ruff check and git diff --check pass.
- NRA before/final: **exact_compact_global, 79 detectors, 0 omitted** with all
  src as dependency context. The duplicate ViewPredicate/SavedView to_wire
  finding is gone. Only ThreadStatus case recovery remains in selected reports,
  owned by Pascal. Cached intermediate `nra-after.json` was partial (43/36),
  so a fresh final global scan was run; it is not treated as a complete audit.
- `focused-tests.txt` retains the first result: 124 passed, one new test failed
  because it expected inputSchema instead of the actual parameters key. The
  corrected current-contract test passes in the final 139-case run.

Commands (reused test environment, no dependency installation):

```sh
PYTHONPATH="$PWD/src" timeout 60 /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python -m pytest -o addopts='' tests/test_saved_view_boundary.py tests/test_channels.py tests/test_s4_ownership.py tests/test_channel_display.py tests/test_exporting.py tests/test_field_codec.py tests/test_tools.py tests/test_declared_family.py tests/test_historical_views.py -q
PYTHONPATH="$PWD/src" timeout 60 /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python evidence/view-serialization/cli_acceptance.py
```

Semantic edits were applied directly: deleting adapters and moving tag-relation
behavior to declarations required no new codemod recipe or codec mechanism.
NRA is ownership analysis here, not a native equivalence certificate. Behavioral
evidence is the local tests and fresh CLI execution above. CI is deferred.

## Parent integration

Merge the full draft branch; build/install through the parent's existing serial
integration path. No native package, provider, active-route or user-data migration
is part of this change. Parent retains deployment. There is no remaining view
caller or product blocker identified in this scope. Owned NRA caches and CLI
fixture directories were removed after verification; receipts/scripts retained.
