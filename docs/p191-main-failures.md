# P191 — main-suite checkpoint before the 13:00 switch

Owner: core-read-locks-2. Deadline 13:10 EDT, 2026-10-17. This is unfinished work, not an acceptance report.

## Execution

Fresh pristine main snapshot: `d473dbe16ee8583a6e46f6e01f52f0f5003f1a11`, `/home/ts/wt/core-p191-main-failures` (now detached, same source). Implementation branch `fix/p191-main-failures` is in the finished isolated `/home/ts/wt/core-read-locks-2` checkout, so edits cannot contaminate that baseline execution.

One serial suite execution is running, PID **966617**, with an explicit immutable native bundle and existing installed dependencies. No dependency installation, new tests or live-root writes. Output: `/home/ts/.cache/agent-scratch/core-p191-suite/execution-suite.log`; final JUnit: `execution-suite.xml` beside it. The execution command and dependency roots are in `execution-run.json`; owner/resource notes in `OWNER.json`. Preserve this process and its private inputs, do not restart/replay it. Current collection: **3590 tests, 6 errors**, continuing into the tests. No completed pass/fail total yet.

Two setup attempts executed no tests: the old dev environment lacked aiohttp and stopped at collection; using runtime dependencies first then failed on an optional mismatched platformdirs pytest plugin. Their complete logs remain `suite.log` and `configured-suite.log`. The actual execution uses existing dev dependency precedence and the immutable installed runtime packages only for missing dependencies. No assertion or skip was changed to get past collection.

Disk was critically low (2.4 GiB); this is one worker, no build or package copies. RAM available was 14.5 GiB. No extra agent was launched.

## Source reading completed; no test edits yet

* 13 original native-shutdown failures were actually missing `PI_COMPACTION_TEST_PACKAGE`, not demonstrated regressions. Their tests and `PiChild.shutdown/_shutdown`, retirement states and fixture were read. The correctly pinned suite will determine their actual result; leave passing tests untouched.
* `RuntimeInfoStore.set` was removed by `c2f3f620ee82376e16fe3f8611c1ef30452cdba7`; the failing RuntimeInfoStore parameter still calls it. The SharedLedger parameter passes and must remain unchanged.
* The saved-runtime rename/date case ends by calling deleted `RuntimeInfoStore.remove`. Current owner is `remove_threads`; keep the date and no-op-write assertions intact if migrating that call.
* Five checkpoint recovery cases finish by constructing `ActivityCheckpointStore` without its now-required `source_path` (introduced in `baf0c9747`). Their recovery behavior remains meaningful. Use the existing reopened ActivityLog's checkpoint owner, preserving every assertion, rather than deleting recovery coverage.
* Three context tests call deleted whole-history `WireLog.context_manifests/context_manifest_resources`; bounded `context_manifest_page` replaced them in `3155aa28a6ed5d139b5a3ce964895a259473072a` (`wire_log.py:429`). The installed callback test also relies on the deleted whole-history API and a manually supplied original root, and asserts it is running site-packages while collected in the source suite. Read all four bodies; remove obsolete structural tests rather than restoring old APIs or loosening assertions.

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

Next after switch: inspect retained suite outcome (never launch it again as investigation); implement the coherent obsolete-test deletion/current-owner call migration, confirm affected cases once, merge with ratchet 0, remove the pristine suite worktree only after the process and children have finished. No implementation or merge is claimed at this checkpoint.
