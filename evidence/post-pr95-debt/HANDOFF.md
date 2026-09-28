# Post-PR95 audit and original S7 manual ownership — Darwin

Persistent tree `/home/ts/wt/comms-post-pr95-compaction-20260928`, branch `codex/post-pr95-compaction-ownership-20260928`, based on completed C0 declaration head6ed78c5. Parent owns C0 combined integration and installation; this worker made no shared/live changes.

Read DECISION.md for the full actual-owner trace, justified existing boundaries, implemented original omission, three concrete pending scopes with migration/deletion/acceptance and assignment status, and issue107 exclusion.

## Implemented independent original omission

ManualCompaction owns one explicit transaction: input configuration, saved source, private no-retry profile/environment, child/RPC channel/stderr task, outcome and cancellation-safe cleanup. Existing session_writer_fence protects its full lifetime. GetState/Compact use existing typed PiRpcChannel/PendingRequests. One instance can run once; uncertainty never authorizes replay.

Deleted manual compact_session/_compact_session_under_fence, duplicate _response and forwarding _summary, plus unused backend.compact_session and its only obsolete-API test. Migrated the production manual bridge call and every current core/test caller. No internal aliases or parallel store/dispatch. Current paired Toad src/tests have no caller of the removed functions; its backend tool_kind import is unaffected.

Changed production files: manual_compaction.py, manual_compaction_bridge.py (constructor/run call only), backend.py (unused function deletion only). Changed tests: manual owner/boundary/bridge/fail-closed, ACP manual cases, native session-reopen interception hooks and obsolete backend case removal. Adaptive PR95 source/journal/native protocol/process watchdog untouched. No native bundle/model changes.

## Focused local evidence

Common cwd this tree; absolute PYTHONPATH="$PWD/src"; existing interpreter `/home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python`; pytest `-o addopts=''`,60s shell bounds and owned persistent `.audit-work` basetemp. No CI wait.

- unit.txt:22 passed,10 skipped. Files test_manual_compaction_fail_closed.py, test_manual_compaction_bridge.py, test_acp_compact_command.py, test_native_owner_launcher_resolution.py, test_native_session_reopen.py. The ten existing optional native-package reopen cases were not configured/run; no claim of fresh installed/native acceptance.
- boundary.txt:22 passed,10 deselected. test_manual_compaction.py selected with `-k 'manual_summary or large_saved or arguments_are_safe or failure_exposes or private_compaction_profile or carries_private_auth or unsafe_args or guard_fsync or profile_commit'`. No loopback/real provider acceptance rerun.
- acp.txt:5 passed,90 deselected (`test_acp.py -k compaction`): real ACP prompt/update owner lifecycle, busy refusal, cancellation and result metadata using the new owner interception contract.
- local-child-final.txt:6 passed (test_manual_compaction_owner.py). Real Python RPC child plus real Node preflight using a local fixture SessionManager, no provider network: saved prefix retained, exact correlated typed commands, blank instructions omitted, committed row required, wrong command rejected, timeout/cancel reaped, profile removed and no same-object replay. Earlier five-case run is retained as local-child.txt, not counted twice.
- Total current executed focused cases55 passed. All completed with exit0. No production failure remained; no broad optional suite performed.
- nra-before.json/nra-after.json: exact_compact_global,79 detectors,0 omitted,complete,0 findings. Full src dependency context with manual/bridge/journal/prepare/commit/runtime/process/provider report targets. Manual ownership findings are reasoned from actual state/caller duplication even though these detectors report zero; no synthesized equivalence proof claimed. Final formatting/blank-instruction omission preserves the old external request behavior and is covered by the final child cases.
- Ruff I/F and git diff --check passed. Exact scan command: timeout60 NRA Python -m nominal_refactor_advisor --json --json-payload summary --parse-workers2 --analysis-workers2 --scan-budget-seconds45 --cache-dir .audit-work/nra-{before,after} --context-root src, followed by the eight report targets above under src/agent_comms. Each scanner ran once.

## Parent integration / remaining limitations

Base is179 so this draft initially targets its branch for a focused diff while parent merges C0. Retarget to main after179 is integrated. Preserve Pascal178 component accesses in manual_compaction_bridge; this change only replaces the inner writer call with ManualCompaction(...).run(). Existing Toad `/compact` RPC/result contract is unchanged. No new paired UI work required for this surface.

Parent folds DECISION.md into dispatch POST-FEATURE-DEBT/index: D1 implemented; D2 journal families, D3 typed native witness and D4 detached summary/string adapter deletion remain pending, not assigned/implemented merely by this audit. Their native receipt and UNKNOWN boundaries remain unchanged. Issue107 remains issue-only.

Canonical pi-native/pi-comms-native explicit manual compaction still requires a real journal-aware manual admission design; it remains refused rather than bypassing native authority. Automatic adaptive PR95 stays default-on. This transaction replacement is not a claim to enable that manual feature or to revalidate stock Pi against a provider.

No providers, paid tests, extra agents/models, source-history reads/replays, live data mutations/restarts or package installs. Owned disposable scanner/test artifacts cleaned after retaining evidence.
