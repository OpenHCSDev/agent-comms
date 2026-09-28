# S13 native history acceptance — manager layer passes; full chain still open

Source: existing tests/test_owner_compaction_prepare.py and stack/test-native-writer-coverage.mjs. Parent assigned production changes to Darwin/S9 and Pascal/S10; no production mechanism added here.

## What is implemented

- Opt-in 288MiB/576MiB real v3 history, generated incrementally on owned disk. No retained user history or provider request.
- Actual native CLI get_state/get_messages, two branches with distinct compactions, settings inherited before retained floor, historical entry lookup and old commit-ID replay refusal.
- Late malformed JSON/UTF8/incomplete/duplicate records; failed loads cannot repair/truncate the source. Existing native writer probe owns these assertions.
- Actual Python owner preparation -> journal/inherited authority native commit -> strict reopen -> fresh native CLI reopen. Original prefix SHA must be preserved; commit only appends.
- Test-only native observer enforces128MiB V8 old space, samples exact process identities every10ms, and retires native processes exceeding240MiB RSS. Kernel high-water reports and Python owner high water are retained. Python over-budget interrupts this test runner for cleanup. Watchdog sampling can overshoot between samples; measured peak must still pass the240MiB assertion. These are test budgets, not new production size ceilings.
- Generated histories/indexes/config/observer scripts are under the test-owned root and removed in finally; tiny JSON receipt and textual logs remain. Disable core dumps during constrained native children. Run serially only.

## Verified before implementation

Configured bundle: /home/ts/wt/comms-refactor2-s9-20260928/stack/.pi-native-50e477b6db64167e/node_modules/@earendil-works/pi-coding-agent (existing prepared package; no native install or edits).

- before-entry-store.log: actual302,041,843-byte fixture reaches real CLI and fails with `Native session file is not a bounded regular file`. Native exit RSS155,028KiB; sampled VmHWM156,060KiB. Generated data removed. This is a production failure reproduction, NOT capacity acceptance.
- fixture-cli-smoke.log: same generator with a small history reaches actual CLI get_state/get_messages, correct retained summary and excluded historical/alternate branch payloads, PASS. Native exit RSS157,796KiB; sampled VmHWM158,352KiB. Zero model requests.
- New writer regressions duplicate-entry-id and forward-parent-id each PASS against native SessionManager. Owned fixture directories removed.
- Initial observer syntax error fixed; failed receipt retained separately, never counted as production evidence.
- Ruff and node syntax checks pass. Existing143MB manualACP/installed-wheel pass was not repeated.

## Run when integrated native artifacts are supplied

Use PYTHONPATH=src, TMPDIR under this worktree, PI_COMPACTION_TEST_PACKAGE and AC_NATIVE_STACK_BIN pointing to one matching prepared package/manifest, AC_NATIVE_LARGE_HISTORY=1.

`python -m pytest -o addopts='' -n0 -q -s tests/test_owner_compaction_prepare.py -k large_history --basetemp=<owned worktree artifact directory>`

Current committed source still rejects at the native256MiB guard. Next: adopt Darwin's actual public EntryStore callers as they land, run both sizes through full chain, retain RSS/disk receipts and review old loader/caller deletion. The baseline did not yet reach prepare/commit/reopen or the large-history branch/replay probe.


## Indexed native manager checkpoint

Actual current SessionManager artifact from Darwin PR243 checkpoint e95775a (its private generated package, not a deployed/manifest-approved build):

| Actual history bytes | Native VmHWM | Python VmHWM | Result |
| --- | --- | --- | --- |
|302,041,814|149,996KiB (146.5MiB)|72,936KiB|PASS|
|604,009,725|186,416KiB (182.0MiB)|72,892KiB|PASS|

Both use128MiB V8 old-space/240MiB RSS test envelope and the real native SessionManager. Both validate two branches/retained summaries/settings, historical payload access, old commit-ID replay refusal and four malformed late records, with exact source fingerprint preserved. All generated session/index data removed after children exit. This is manager-level acceptance; **actual integrated CLI/prepare/journal commit/reopen on >256MiB is still pending SDK/helper/proof caller composition**.

Race probe migrated to actual readSync observation boundaries, eliminating deleted loadEntriesFromFile/_loadEntries/preloaded-array dependence. Current indexed snapshot-race and set-session-race PASS. Empty-file initializer race FAILS: the returned manager has a different session ID from the file another real process initialized. The probe accepts either typed refusal or a coherent reload; it is not pinned to a particular refusal message. Darwin received the actual mismatch evidence on PR243. Review also flagged dead _rewriteFile, source fork fencing, direct store file validation and abnormal-exit index cleanup. Source0d900f3 now uses initialized SQLite EXCLUSIVE+unlink; end-to-end/abnormal-exit verification of that revision remains to be done.

Observer correction: Linux RUSAGE_SELF can retain a high water from the tool launcher before exec (607,604KiB with actual VmHWM15,400KiB observed). Tests now use /proc/self/status VmHWM for the current executable; Node resourceUsage is recorded separately. The false observer failure is retained as observer-inherited-rusage-failure.log and is not a production failure. Node/kernel readings above are current VmHWM.
