# PR95 completion checkpoint — September 27, 2026

Owner: PR95 continuation in `/home/ts/wt/comms-pr95-finish-codex-20260927`,
branch `codex/pr95-finish-20260927`, preserving prior commits and recovered dirty work.
Implementation: `351a4930b3bf0bdebeed9178f0e5bee286a9e1c0`, published as
[draft PR135](https://github.com/OpenHCSDev/agent-comms/pull/135) into PR95.
This section supersedes the historical remaining-work checklist below.

Implemented: selected-child effective-settings RPC; canonical/custom-model and
project-trust integration; continued private-session coverage derived from the
existing ACP starts and immutable live-recorded native results; complete-package
runtime verification; and adaptive compaction enabled by default for verified
native ACP owners. Ordinary turns no longer require an active goal. All original
input admissions still bind exactly once. Unresolved inputs/operations still
refuse compaction; no row is removed or silently promoted to settled.

The normal compiled bundle is `stack/.pi-native-252003c2a59db46a`. Both normal preparation
and the active `/var/tmp` route verifier accept the same full manifest. The actual
services factory retains canonical/project compaction settings and disables
tracked session/provider retries in SettingsManager, including after reload and
project-trust changes. Tracked automatic/overflow summaries remain disabled;
the owner journals the selected summary before its original input is sent.
The active launcher uses the copied, verified import fence and project bootstrap.

Validation is local/offline: actual Pi SDK/RPC repeated summary and clean-decline
cycles, strict reopen, exact-once originals, private continued history, no-goal
owner, custom model, disabled settings, corrections, and summary retention in the
next provider context. Queue/restart gates pass. The isolated wheel includes the
same manifest and verifies the built bundle. See `pr95-evidence/VALIDATION.md` for
commands, counts, NRA coverage, and precise limits.

Integration/deployment remains parent-owned. No live root, original UNKNOWN
input, owner process, Toad, coordination_store.py, coordinated_runtime.py, or
native failure-recovery test was changed. No live provider calls or CI wait.
No idle selected child or unavailable matching context telemetry remains a clean
trigger skip; this does not authorize a summary/retry or clear uncertain history.
Untracked legacy private histories fail closed rather than being auto-enrolled.

Merge this branch normally into PR95's integration branch. Preserve parent
recovery changes. S8 integration needs only the nullable goal snapshot in the two
existing ThreadRegistry compaction methods and ACP's default-on constructor;
S4's read/routing methods are untouched. Refactor slots S8 and S4 were resumed
in their original CLI sessions, and actual process/progress records are in
`/home/ts/wt/comms-refactor-dispatch-20260927/README.md`.

---

The earlier checkpoint below is retained as implementation history.

# PR95 integration checkpoint — September 27, 2026

Worktree: `/home/ts/wt/comms-pr95-finish-codex-20260927`.
Branch: `codex/pr95-finish-20260927`.
Original PR95 and saved PR48/Pi-owner worktrees are preserved. Main is normally
merged through PR126 (`e4cd10e`), preserving configured model/auth routing.

## Implemented

- `b74774f` implemented the selected-summary RPC exchange with the owner's
  existing idle Pi child, durable reservation before stdin, bounded reads,
  native usage/file operations, and UNKNOWN retirement without retry.
- Normal `stack/bin/prepare-pi-native` now includes readiness and selected-summary
  RPC patches plus the parent's `patch-native-model-config.py` from PR126.
  The combined file manifest and whole-package commitment are updated; normal
  preparation succeeds. Canonical auth/models and isolated retry settings retain
  their separate directories. Parent still owns the Python runtime route changes.
- `maybe_compact_owner_turn` invokes this selected exchange through the existing
  owner compaction runtime. `SelectedNativeSummary` owns reservation linkage and
  original-input admission; ordinary NativeSummary behavior remains unchanged.
- The owner bridge checks the reservation/source, commits through its existing
  native writer, embeds the selected operation/source digest in the commit, and
  links the durable committed result before returning the existing one-use
  admission token. Owner/input/settings/session revisions are checked again.
- ACP installs that returned token at its existing final native-ID bind boundary.
  Compaction now observes the final prompt after passive-awareness augmentation,
  so the bound digest matches the actual input. Parent cursor methods are intact.
- New offline integration host uses the actual prepared Pi SDK/RPC with synthetic
  model output and prohibited network access. It exercises selected summary,
  native commit, strict fresh reopen, original input proof, settlement and backend
  reuse. This is local protocol evidence, not configured credential/provider or
  real-model retention evidence.

## Local verification

The earlier RPC slice passed 66 Python tests and 42 native synthetic-stream cases.
Current focused unit selection: **89 passed, 1 optional fixture test skipped**.
Native integration: **6 passed**; adaptive/runtime: **14 passed**; strict reopen:
**10 passed**. Total current selection: **119 passed, 1 skipped**. An initial
combined run hit its 60-second deadline after 28 passing cases; the complete
smaller runs above supersede that incomplete result.
Main-merge checks: 139 passed and 10 optional tests skipped initially; one
existing inode-replacement fixture failed because unlink immediately reused its
inode. The fixture now renames its original before creating the replacement;
all five affected cases pass. No runtime check was weakened.
Ruff and diff whitespace pass. No live provider, install/restart, shared checkout
edit, or UNKNOWN replay. CI was not awaited.

Reproduce normal preparation:

```sh
stack/bin/prepare-pi-native
```

Set `PI_COMPACTION_TEST_PACKAGE` to the resulting
`stack/.pi-native-<manifest-sha256-first-16>/node_modules/@earendil-works/pi-coding-agent`.
With `PYTHONPATH=src`, run pytest with `-o addopts=''`, a persistent worktree
`--basetemp`, and these bounded groups separately:

- `tests/test_selected_owner_compaction_integration.py`
- `tests/test_owner_compaction_adaptive.py tests/test_owner_compaction_runtime.py`
- `tests/test_native_session_reopen.py`

The new integration test verifies a duplicate send is denied, a correction
before commit leaves the original unbound, and a correction after native commit
prevents minting admission. Successful real SDK/RPC reopen stores exactly one
original input and retains a settled reusable child.

## Remaining requirements — PR95 is not complete or live

1. DONE: selected-attempt lifecycle. Barrier retirement derives from the existing
   input ledger's exact native start (owner, turn, input, admission and prompt
   hashes). Historical attempts stay intact and cannot replenish tokens. Bound
   UNKNOWN and mismatched starts continue blocking. Actual offline Pi SDK/RPC
   passes two complete compaction/commit/reopen/original/settlement cycles on the
   same continued session; next reservation and new inputs remain usable.
2. Continue ordinary private sessions safely: fresh-only enrollment and the raw
   UNKNOWN coverage floor still exclude continued `native-sessions` histories.
   Preserve unresolved inputs and existing journals; never delete rows to bypass.
3. DONE: clean prestart declines use the existing decline admission path after
   revalidating owner, ingress and unchanged saved source. Summary and decline
   are nominal OwnerSummaryOutcome cases; the decline performs no writer call
   or manager retirement. Other errors and UNKNOWN remain blocked, without
   replay. Four real/synthetic RPC decline/correction cases pass, including
   repeated originals after unchanged-source declines.
4. The active route still verifies the smaller native `_PATCHED_SHA` set, whose
   RPC/session-manager hashes predate this full compaction bundle. Reconcile that
   existing runtime verifier with the normal bundle before switching its route.
5. Effective project/custom-model configuration is still excluded by the adaptive
   owner admission; integrate the parent-owned runtime settings/model route.
   No active goal or no existing idle child is currently a trigger skip.
6. Enable the completed route by default, verify repeated compaction/retention
   and queued inputs, then parent performs merge, installation and live checks.
   Adaptive activation remains off until these substantive gaps are closed.

## Integration

Fetch and normally merge `origin/codex/pr95-finish-20260927` into PR95's integration
worktree. This retains original PR95 and main ancestry; no force push required.
If integrating only the current slice, apply it after `b74774f` and main PR124.

PR126 parent's patcher was copied unchanged into this branch. Preserve parent
`native_pi.py`, `coordinated_runtime.py`, source configuration environment and
additional channel tools/relevance changes. PR126 is already normally merged.
The normal combined bundle's `agent-session-services.js` hash is
`4af410d793207f0269cf442a799b0f83933b69d728d166e49a3a6134ff7108a6`.
Its RPC includes both selected-summary operations and native input proofs.

Only ACP's selected-admission map comments, existing compaction call, and ordering
relative to passive-awareness augmentation changed here; cursor methods did not.
S1's parallel event refactor also touches `_run_agent_turn`: preserve this
compaction placement/callback when merging, adapting test event fixtures to its
new event classes. Backend changes since `b74774f` are absent.

Source leases saved in the PR48 worktree remain untouched. No deployment or
activation is implied by this checkpoint.

## Subsequent lifecycle implementation

`SelectedSummaryAttempt` owns the original-start predicate; every journal send,
reserve and commit guard uses one shared projection. No new store, state enum,
replay recovery or duplicate input authority. The input ledger is read once per
projection rather than once per historical summary; sessions without summaries
need no input-ledger scan.

Validation: selected journal/admission tests cover completed linked/declined
attempts, UNKNOWN and mismatched starts, second reservation/commit, historical
retention and used-token refusal. Real offline SDK/RPC repeated-cycle test passes.
This change is limited to compaction_journal.py, compaction_send_admission.py and
focused tests, so S1's ACP/event migration is untouched.

## Clean-decline completion

`OwnerSummaryOutcome` is the public behavioral contract. NativeSummary owns the
shared commit behavior inherited by SelectedNativeSummary; SelectedSummaryDecline
preserves its source and obtains the existing one-use decline admission through
the owner bridge. No fake summary, duplicate writer or retry path is introduced.

Local verification after this change: 10 owner integration cases pass (4 decline,
6 summary/correction cases); 14 adaptive/runtime cases pass, including cancellation
joining the actual commit worker. Both native success and clean-decline cases
exercise repeated originals on the actual offline prepared SDK/RPC host. No
live provider/install/restart or ACP/S1 file edit.
