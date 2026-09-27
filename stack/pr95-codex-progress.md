# PR95 integration checkpoint — September 27, 2026

Worktree: `/home/ts/wt/comms-pr95-finish-codex-20260927`.
Branch: `codex/pr95-finish-20260927`.
Starts at original PR95 `4a10699`; normally merges current main through PR123.
Original PR95 and the saved PR48/Pi owner worktrees are preserved.

## Implemented

- `SelectedSummarySlot.run_selected_summary` now exchanges the existing v1
  summary protocol with the owner's already selected, idle Pi process.
  It checks saved session and sidecar revisions, keeps both borrow locks,
  durably reserves the operation before writing, and returns native summary,
  file operations, and usage. It never starts another provider process or
  sends the original user input.
- Timeouts, malformed responses, cancellation, and saved-source changes
  leave a durable UNKNOWN and retire the borrowed child. A complete response
  leaves its reservation for exact commit linkage. A decline is data; it
  does not silently reopen original-input admission. No automatic retry.
- The existing JSONL reader supports a caller-supplied record bound, including
  fragments retained across cancellation. Ordinary event readers keep their
  existing behavior.
- Imported the existing native readiness/summary implementation and patchers
  from `feat/pr95-selected-pi-summary-20260926` (`db39f98`). Its synthetic-stream
  fixture now also exercises the Python adapter against actual patched Pi RPC.
  There is no replacement native summarizer implementation.
- Native package resolution recognizes the installed `pi-comms-native`
  entrypoint and its aliases using the existing route owner. Full package
  verification still applies. Manual compaction cannot route that entrypoint
  through the unrelated legacy writer.

## Evidence

- Focused Python suite: 66 passing tests, including actual Python-to-patched-Pi
  RPC with native `compact()` and a synthetic stream, real subprocess pipes,
  and durable SQLite. No provider/network calls.
- Existing native summary suite: 42 cases passed against an owned copied Pi
  package with the existing production writer patch. Source stayed unchanged;
  tested success, bounded map/synthesis, cancellation, mutation exclusion,
  retries disabled, and uncertain stream termination.
- Ruff on changed production Python and tests; `git diff --check` pass.
- No shared checkout, live runtime, live provider, or owner process was changed.
  No UNKNOWN inputs were replayed. CI was not awaited.

Reproduce the Python slice with `PYTHONPATH=src python -m pytest -q -o addopts=''`
and the files `test_selected_summary_exchange.py`, `test_selected_pi_route.py`,
`test_selected_summary_journal.py`, `test_selected_pi_summary_rpc.py`,
`test_native_owner_launcher_resolution.py`, `test_manual_compaction_bridge.py`,
and `test_selected_summary_guardian_combined.py` under `tests/`.
For the optional combined native test, set `PI_NATIVE_PACKAGE_DIR` to an owned
copy of the current Pi package after applying
`patch-native-session-writer-prototype.py --production` to its session manager.
Create `.pr95-disposable-test-copy` in that package containing `owned fixture`
and a newline. Keep `TMPDIR`/pytest `--basetemp` inside an owned worktree.
`node stack/test-native-selected-compaction-summary.mjs` runs the native matrix.
Never use a running package as this fixture: the fixture stages patched RPC
files next to the package's original RPC module and cleans them afterwards.

## Remaining requirements — PR95 is not complete or live

1. Build one consistent native bundle: the current live copied package fails
   PR95's complete-tree commitment, and its session manager lacks
   `captureCompactionWitness`. Its seven-file N/K pin passing does not supply
   the PR95 writer. The copied fixture needed the existing production writer
   patch before native summary tests could run. Native summary/readiness RPC
   patchers are now present on this branch but are not installed by the normal
   package preparation path yet. Update that path and its resulting manifests.
2. Connect this exchange to `maybe_compact_owner_turn` and the existing owner
   commit bridge. The current owner bridge does not bind
   `selectedSummaryOperationId`/source digest into a native commit. Add that
   integration so a successful reserved summary can link one committed result,
   publish its metadata, and admit its original input exactly once. Merely
   calling this adapter from ACP would leave a blocking reservation.
3. Finish usable session coverage: selected source capture still pins the older
   preparation module; fresh-only private enrollment and the raw-history floor
   exclude ordinary continued sessions. Preserve existing uncertain attempts;
   do not erase journal rows to enable compaction.
4. Verify selected configured-model/route behavior and cancellation/retirement
   through the complete owner path. The tests here use synthetic streams and
   are not credential/extension parity evidence or real model retention tests.
5. Enable the completed adaptive owner route by default, run repeated compaction
   retention/correction/queued-input cases, then parent performs normal merge,
   installed-runtime checks, and activation once bus work is stable.

## Integration

From a PR95 integration worktree, fetch and normally merge
`origin/codex/pr95-finish-20260927`. This contains the original PR95 history
and a normal main merge; no force push or branch replacement is required.
The changes do not touch `acp.py`, Toad, or the parent's cursor methods.
Backend changes are confined to `_JsonLineReader.readline`'s optional bound.
The source lease saved in the PR48 worktree remains untouched; it is not a
substitute for completing ordinary-session compaction.
