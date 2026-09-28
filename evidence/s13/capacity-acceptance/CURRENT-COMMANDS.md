# Current parent receiver acceptance commands

Run in `/home/ts/wt/comms-refactor2-s13-20260928`, source1aef899 plus the reopen test caller migration. Reuses existing runtime/package; no installation or provider call. Every temporary root stays under this worktree. Large test finally blocks remove generated history; remaining small pytest artifacts are removed after receipts are retained.

```sh
export PYTHONPATH=src
export PI_COMPACTION_TEST_PACKAGE=/home/ts/wt/comms-native-session-entry-store-20260928/stack/.pi-native-0d7ebb4f4b5aa1ec/node_modules/@earendil-works/pi-coding-agent
export TMPDIR="$PWD/.artifacts/s13-parent-capacity"
S13_PYTHON=/home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python
mkdir -p "$TMPDIR"
"$S13_PYTHON" -m pytest -o addopts='' -n0 -q tests/test_owner_compaction_prepare.py::test_native_preparation_is_read_only_and_matches_saved_cutpoint tests/test_owner_compaction_prepare.py::test_canonical_owner_prepares_source_before_summary_and_commits_once tests/test_native_session_reopen.py::test_strict_native_reopen_preserves_bytes_and_strips_preload tests/test_canonical_manual_compaction.py::test_actual_acp_compact_uses_journal_and_reports_saved_history --basetemp="$TMPDIR/focused"
AC_NATIVE_LARGE_HISTORY=1 "$S13_PYTHON" -m pytest -o addopts='' -n0 -q -s 'tests/test_owner_compaction_prepare.py::test_large_history_cli_prepare_commit_reopen_under_memory_budget[288]' --basetemp="$TMPDIR/large-288"
AC_NATIVE_LARGE_HISTORY=1 "$S13_PYTHON" -m pytest -o addopts='' -n0 -q -s 'tests/test_owner_compaction_prepare.py::test_large_history_cli_prepare_commit_reopen_under_memory_budget[576]' --basetemp="$TMPDIR/large-576"
```

Native launcher supplied by Darwin remains `.../comms-native-session-entry-store-20260928/stack/bin/pi-native`; current helper APIs accept the verified package directly. No launcher/225 receiver shim is part of these runs.
