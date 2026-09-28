# PF5 — historical display/index ownership

2026-09-28 · Pascal · source complete; parent owns serial integration and deployment.

## Source / ownership

- Tree `/home/ts/wt/comms-pf5-historical-display-20260928`; branch `codex/pf5-historical-display-20260928`, based on main208 `651456f`.
- Assigned requirement: dispatch `plans/POST-FEATURE-DEBT-AUDIT.md` PF5. Parent/Darwin retain the priority ACP startup fix, native launcher/extensions/backend diagnostics. This branch does not touch those files or implement PF1–PF4.
- Existing `ChannelDisplayScope` now determines membership capture and safe index narrowing as well as inclusion. `DMDisplayScope` owns the two original alias sets and their directional pair predicate. Both implement public `MessageDisplayScope`; no enum/string case dispatch or parallel index/store.
- Historical `HistoryView` requests bind once to each source registry: `ChannelHistory` retains exact target selection; `ChannelDisplayHistory` binds current channel metadata to original source membership; `DMHistory` binds original alias names. The captured scope owns decisions during page iteration; these objects do not borrow Comms/shared-self state. They are process-local query values, not a new persisted protocol/compatibility projection.

## Actual migration / deleted mechanisms

- `read_basis.py`: ChannelDisplayScope.capture and index_targets; DMDisplayScope capture/inclusion/prefilter; shared public MessageDisplayScope contract. Any-mode returns no target prefilter: sender/peer/mention inclusion remains authoritative.
- `historical_views.py`: nominal HistoryView/source-binding cases replace per-row callback protocols.
- `history_views.py`: all five integrated consumers (DM history, DM display, channel history, channel display, full history) supply actual views. Delete the repeated canonical-name closures and per-message membership/alias/target sets. Live channel display-basis capture now uses the same ChannelDisplayScope owner. Live snapshot/read-ack boundaries remain intact.
- `message_bus.py`: historical_page binds once per source and uses that scope through display_page; original registry instance reused, source revision validation retained before page access. Live DM/channel/full-history pages also use the captured owners. Delete duplicate live DM lambda and target/all-row predicate lambdas.
- Retired public `MessageBus.channel_display_page` is replaced in place by generic scoped `MessageBus.display_page`, which applies scope.index_targets through existing `_history_page` and `BusPageIndex.offsets`. All current callers migrated; no old-name wrapper. `HistoryViews.channel_display_page` remains the public channel UI route.
- `BusPageIndex` implementation/schema, canonical row validation, stale-offset fallback and wire locks are reused unchanged. Cold index validation still decodes every authoritative row once. Any-mode stays an unrestricted candidate scan; this closes ownership without silently dropping out-of-channel mentions or inventing a second index.
- Tests: new `tests/test_historical_page_scope.py`; migrate the one direct bus display caller in `tests/test_read_ledger.py`. No production Toad direct consumer of the removed bus method or historical callback API was found; no paired source patch required.

## Acceptance at its actual strength

- `current-seams.log`: **76 passed** — existing historical pagination/provenance/ack, index invalidation/fallback, channel display/any-mode/read-ledger and channel behavior.
- `scope-parity-accepted.log`: **81 passed** —80 cold/warm cases across sparse channels, any-mode sender/peer/mention, builtins/alias, absent matches, DM/source aliases (different live alias ownership), forward/backward cursors and byte budgets; one scope-capture/decode-count case.
- `full-traversal.log`: **8 passed** — complete forward/reverse traversal for those eight view kinds, with exact message/order equality and bounded termination.
- `decode-counts.log`: **1 passed**, repeats only the existing count case after adding the explicit unfiltered control. Same two-row page: **141 Message decodes without prefilter,3 with it**; cold index243 =240 canonical validations +3 candidates; source capture once. No elapsed-time acceptance threshold.
- **165 distinct core cases passed**, not166; the strengthened count case is a repeat.
- `original-comparison.log`: **41 original copied page cases equal main208**, both cold and warm. Captures public rows, provenance/view keys, oldest/newest source cursors and both edge flags. #comms/#nra/#all/#any/#none/broadcast, display routes and an actual saved DM pair, with small/large byte budgets and before/after boundaries. All comparisons used owned display-only copies of two preserved original history sources; no private marker transplantation/live writes. Raw copied content/baselines were removed after comparison, not published in this PR.
- `mounted-channels-accepted.log`: **process exit0** using installed Toad with this source core: original #comms20/#nra8 rows mounted, copied bus sequence unchanged. No messages sent or provider calls; all XDG/project/root writes confined to owned fixtures.
- `git diff --check` passes. NRA before/after: **79 detectors,0 omitted, complete exact_compact_global,0 findings**. These scans do not certify behavior; the page and UI receipts above do.

## Failed evidence / harness corrections

The three `scope-parity*.log.gz` failure receipts remain intact: initial new fixture reused an explicit creation identity, then omitted a live alias needed for the public DM entry point, then incorrectly expected raw-collector edge flags on terminal empty historical pages. Fixed fixtures/oracle preserve established historical traversal semantics; no production relaxation. The accepted matrix/traversal results supersede those failed attempts.

First `mounted-channels.log` mounted both channels and printed the sequence assertion but hit timeout124 during background executor drain; it is not a full process pass. Its copied fixture omitted the native unread cache. Copied the existing20MB SQLite cache read-only; all84 cached native source revisions matched. `mounted-channels-warm.log` then failed its scroll-edge wait: the harness had not released follow-tail anchoring or awaited the existing edge/refresh owner. Final harness follows the production pilot's release-anchor/scroll/edge sequence, waits readiness, and exits0. No production Toad or unrelated native/unread code was changed.

## Reproduction

Existing core integration venv; xdist/coverage defaults disabled; temporary fixtures under the owned tree:

```sh
mkdir -p .artifacts/tests
PYTHONPATH=src TMPDIR=$PWD/.artifacts/tests timeout 60 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -q -o addopts='' tests/test_historical_views.py tests/test_bus_page_index.py tests/test_channel_display.py tests/test_read_ledger.py tests/test_channels.py
PYTHONPATH=src TMPDIR=$PWD/.artifacts/tests timeout 60 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -q -o addopts='' tests/test_historical_page_scope.py
```

`copied_pages.py baseline` was run at main208 before source edits, then `verify` after migration. It copies only display metadata/bus bytes and the disposable unread cache from the preserved root; its41-case exact baseline lives under `.artifacts`. Run those modes against the respective sources, on the same owned copies. `mounted_channels.py <fresh-config-name>` uses installed Toad plus `PYTHONPATH=src`, the prepared owned copy and isolated XDG/project directories; existing runtime Python was `/home/ts/.local/share/agent-comms/runtime-r6-transcripts-20260928/bin/python`. Failed runs are not acceptance commands to repeat.

Successful full-context NRA invocation, both before/after:

```sh
timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor src/agent_comms/read_basis.py src/agent_comms/historical_views.py src/agent_comms/history_views.py src/agent_comms/message_bus.py src/agent_comms/bus_page_index.py --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload agent --raw-findings
```

These are authored ownership/caller patches, not a claimed proof-producing NRA DSL transformation. No additional full suite/CI/provider proof is required by this handoff.

## Remaining / cleanup

No PF5 source blocker. Parent must serially integrate; live deployment and the ACP startup priority remain parent-owned. PF1–PF4 are not claimed complete. Owned copied buses,20MB unread cache, raw comparison data, isolated XDG/project state and test fixtures removed after all test processes exited; no owned worker remained. Compact success/failure evidence retained. Shared/live worktrees and original user data were not edited.
