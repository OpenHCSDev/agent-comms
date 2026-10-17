# P191 — main-suite checkpoint before the 13:00 switch

Owner: core-read-locks-2. Deadline 13:15 EDT (extended by Claude), 2026-10-17. This is unfinished work, not an acceptance report.

## Execution

Fresh pristine main snapshot: `d473dbe16ee8583a6e46f6e01f52f0f5003f1a11`, `/home/ts/wt/core-p191-main-failures` (now detached, same source). Implementation branch `fix/p191-main-failures` is in the finished isolated `/home/ts/wt/core-read-locks-2` checkout, so edits cannot contaminate that baseline execution.

One serial suite execution is running, PID **966617**, with an explicit immutable native bundle and existing installed dependencies. No dependency installation, new tests or live-root writes. Output: `/home/ts/.cache/agent-scratch/core-p191-suite/execution-suite.log`; final JUnit: `execution-suite.xml` beside it. The execution command and dependency roots are in `execution-run.json`; owner/resource notes in `OWNER.json`. Preserve this process and its private inputs, do not restart/replay it. Current collection: **3590 tests, 6 errors**, continuing into the tests. No completed pass/fail total yet.

Two setup attempts executed no tests: the old dev environment lacked aiohttp and stopped at collection; using runtime dependencies first then failed on an optional mismatched platformdirs pytest plugin. Their complete logs remain `suite.log` and `configured-suite.log`. The actual execution uses existing dev dependency precedence and the immutable installed runtime packages only for missing dependencies. No assertion or skip was changed to get past collection.

Disk was critically low (2.4 GiB); this is one worker, no build or package copies. RAM available was 14.5 GiB. No extra agent was launched.

## Coherent repair after source reading

* 13 original native-shutdown failures were missing `PI_COMPACTION_TEST_PACKAGE`, not demonstrated regressions. All 13 now PASS with the explicit immutable pin, unchanged; actual localhost/native children closed normally. Tests and PiChild shutdown/retirement owners were read. Calling these existing production failures was wrong.
* Removed only the failed RuntimeInfoStore parameter calling deleted `set` (`c2f3f620ee82376e16fe3f8611c1ef30452cdba7`). The passing SharedLedger parameter and assertions remain unchanged.
* Migrated the failed saved-runtime rename/date case from deleted `remove` to current `remove_threads`. One call changed; all date, encoded value and no-op-write assertions unchanged. This case PASSed in the final affected batch.
* Removed five obsolete read-repairs-disk checkpoint cases. An initial owner-call migration exposed the deeper stale assumption: ActivityLog reuses the canonical ActivityReadState when the source revision is unchanged, and only its writer publishes disk checkpoints (`activity.py:314-344,399-413`, `baf0c9747`). Readers correctly returned the original data without repairing a damaged disposable file. Do not restore read-time disk mutation to satisfy this old structure. The affected confirmation recorded 4 failures/15 passes before deleting this five-case test; it is retained, not hidden.
* Removed four obsolete context tests calling deleted whole-history `WireLog.context_manifests/context_manifest_resources`, replaced by bounded `context_manifest_page` in `3155aa28a6ed5d139b5a3ce964895a259473072a` (`wire_log.py:429`). The old installed callback recording also used that deleted API and required a manually supplied original root while being collected in the source suite. No old APIs, skips or relaxed assertions added.

Existing NRA parser census retained at `.artifacts/p191/before.json` in the implementation checkout; covers these tests and their production owners, no second scanner.

## Original 24 failure IDs

```text
tests/test_backend_native_custody.py::test_failed_owned_shutdown_preserves_original_outcome_without_retry[False-False-ChildRetirementCancelled]
tests/test_backend_native_custody.py::test_failed_owned_shutdown_preserves_original_outcome_without_retry[False-False-TimeoutError]
tests/test_backend_native_custody.py::test_failed_owned_shutdown_preserves_original_outcome_without_retry[False-True-ChildRetirementCancelled]
tests/test_backend_native_custody.py::test_failed_owned_shutdown_preserves_original_outcome_without_retry[False-True-TimeoutError]
tests/test_backend_native_custody.py::test_failed_owned_shutdown_preserves_original_outcome_without_retry[True-False-ChildRetirementCancelled]
tests/test_backend_native_custody.py::test_failed_owned_shutdown_preserves_original_outcome_without_retry[True-False-TimeoutError]
tests/test_backend_native_custody.py::test_failed_owned_shutdown_preserves_original_outcome_without_retry[True-True-ChildRetirementCancelled]
tests/test_backend_native_custody.py::test_failed_owned_shutdown_preserves_original_outcome_without_retry[True-True-TimeoutError]
tests/test_backend_native_custody.py::test_resource_release_failure_retains_exact_native_child
tests/test_backend_native_custody.py::test_stop_failure_accounts_for_native_resources_and_retains_uncertainty[RuntimeError-reap]
tests/test_backend_native_custody.py::test_stop_failure_accounts_for_native_resources_and_retains_uncertainty[RuntimeError-release_io]
tests/test_backend_native_custody.py::test_stop_failure_accounts_for_native_resources_and_retains_uncertainty[TimeoutError-reap]
tests/test_backend_native_custody.py::test_stop_failure_accounts_for_native_resources_and_retains_uncertainty[TimeoutError-release_io]
tests/test_runtime_collaboration_documents.py::test_authoritative_documents_keep_bytes_on_failed_publication[RuntimeInfoStore-<lambda>-{"old": {"thread": "old", "ts": 1}}]
tests/test_runtime_collaboration_documents.py::test_invalid_checkpoint_rebuilds_from_unchanged_authoritative_log[<lambda>0]
tests/test_runtime_collaboration_documents.py::test_invalid_checkpoint_rebuilds_from_unchanged_authoritative_log[<lambda>1]
tests/test_runtime_collaboration_documents.py::test_invalid_checkpoint_rebuilds_from_unchanged_authoritative_log[<lambda>2]
tests/test_runtime_collaboration_documents.py::test_invalid_checkpoint_rebuilds_from_unchanged_authoritative_log[<lambda>3]
tests/test_runtime_collaboration_documents.py::test_invalid_checkpoint_rebuilds_from_unchanged_authoritative_log[<lambda>4]
tests/test_runtime_collaboration_documents.py::test_saved_runtime_dates_and_fields_survive_rename_and_reopen
tests/test_turn_context_wire.py::test_context_pointer_family_rebuilds_and_decodes_only_selected_originals
tests/test_turn_context_wire.py::test_context_resources_revalidate_selected_bytes_without_redecoding
tests/test_turn_context_wire.py::test_installed_context_callbacks_share_original_writer_custody
tests/test_turn_context_wire.py::test_recorded_context_history_retains_rename_and_original_predecessor
```

## Collection blockers discovered beyond the original 24

`test_active_turn.py:10` imports deleted FailedPreparingDrainReadiness; `test_detached_owner.py:12`, `test_image_inputs.py:20`, `test_runtime.py:14` import deleted TurnStarted/TurnSettled protocol facts (removed in `4f0758854`); `test_configured_installed_source_arguments.py:12` imports removed publish_retained_summary; `test_native_context_inspection.py:30` gives a parametrized recorded_readers argument a default, rejected by pytest. Do not delete unexamined passing functions in these modules. Report them separately if outside the bounded 24-case repair.

Original 24: 10 obsolete cases removed; 14 retained cases PASS (13 correctly configured native cases, one current-owner call migration). No production behavior regression demonstrated among these 24. No passing test body or assertion changed. Full baseline remains running and has six collection errors plus further failures beyond this assigned set; no green-suite claim. Do not rerun it as investigation. Merge with ratchet 0 and remove the pristine suite worktree only after its process and children have finished; active borrower prevents removal meanwhile.
