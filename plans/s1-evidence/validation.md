# Original S1 validation receipt

**Superseded integration status:** see [integrated-validation.md](integrated-validation.md) for the current main/PR95 tree and executed native bundle. This file retains first-publication history.

All tests ran sequentially, using the existing Python environment read-only, importing this worktree's `src`. Pytest's configured xdist and coverage defaults were disabled with `-o addopts=''`. No full repository suite or CI wait was run.

Final focused results (overlapping files are intentionally reported per shard, not as a unique-test total):

| Shard | Result | Receipt |
| --- | --- | --- |
| Backend, ACP, nominal contracts | 308 passed, 38.64 s | `tests-core.txt` |
| Input identity, queues, compaction, participant | 68 passed, 27.38 s | `tests-input.txt` |
| Goal lifecycle, owner followups, routing | 171 passed, 35.77 s | `tests-goals.txt` |
| MCP, images, diagnostics, native-startup/fence helpers, manual bridge | 80 passed, 30 skipped, 11.02 s | `tests-presentation.txt` |
| Baseline, before producer migration | 17 passed, 2.18 s | `tests-baseline.txt` |

The 30 skips are opt-in prepared-native-stack cases (`AC_NATIVE_STACK_BIN` absent, plus the mounted Toad pilot prerequisite). Their assertions were migrated and collected, but native-stack execution is not established. The real MCP package/SDK test used a fake Pi process and local stdio MCP server; it explicitly makes no model/provider call. Backend tests execute local RPC and text subprocess fixtures, including large records, persistent child reuse, input/turn identity, watchdog aborts, compaction, and redaction.

Ruff passes and Black reports all 42 changed Python files unchanged. NRA receipts separately describe structural coverage. The initial failures and fixes are retained in `initial-failures.txt`: a producer-module shadowing bug was fixed; remaining failures were old dictionary assertions, old request-map/test-hook contracts, incomplete new test fixtures, or the initially absent local Node dependency. No assertions were weakened to accept invalid events.

## Reproduction

From the assigned worktree, create disposable directories and set the common environment:

```sh
mkdir -p .s1-artifacts/tmp .s1-artifacts/cache
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/src"
export TMPDIR="$PWD/.s1-artifacts/tmp"
export XDG_CACHE_HOME="$PWD/.s1-artifacts/cache"
```

Use `/home/ts/.agent-comms/.venv/bin/python -m pytest -o addopts='' --basetemp="$PWD/.s1-artifacts/SHARD" -q` with these file groups, one command at a time (bounded by `timeout 165`):

- Core: `tests/test_agent_events.py tests/test_backend.py tests/test_backend_settlement.py tests/test_acp.py`
- Input: `tests/test_agent_events.py tests/test_acp_input_disposition.py tests/test_acp_channel_disposition.py tests/test_acp_goal_input_authority.py tests/test_acp_goal_original_input.py tests/test_acp_owner_interrupt_followup.py tests/test_prompt_queue.py tests/test_acp_compaction_activity.py tests/test_agent_loop.py tests/test_manual_compaction_bridge.py`
- Goals: `tests/test_acp_owner_interrupt_followup.py tests/test_goal_standby_liveness.py tests/test_goal_standby.py tests/test_goal_direct_interrupt.py tests/test_goal_failure_observation.py tests/test_runtime_goal_retry_running.py tests/test_backend_failure_owner_pause.py tests/test_headless_diagnostics.py tests/test_reply_routing.py tests/test_transcript_input_routing.py tests/test_passive_channel_awareness.py tests/test_user_channels.py tests/test_project.py`
- Presentation: `tests/test_mcp_relay.py tests/test_tool_diffs.py tests/test_image_inputs.py tests/test_native_startup.py tests/test_native_proof_journal_limit.py tests/test_maintenance_barrier.py tests/test_manual_compaction_bridge.py tests/test_manual_compaction_fail_closed.py tests/test_stack_native_compaction.py tests/test_stack_settlement_boundary.py tests/test_stack_send_now.py tests/test_stack_inbox_output.py`

The MCP package tests first need `npm ci --ignore-scripts --no-audit --no-fund --cache "$PWD/.s1-artifacts/npm-cache" --prefix extensions/pi-mcp-client`. The lockfile was not changed. Dependencies were installed only in the worktree and removed after validation.

For NRA, use its checkout on `PYTHONPATH`, the NRA `.venv/bin/python -m nominal_refactor_advisor`, the eight production files in this PR, and `--context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --json --json-payload loop`.

Available RAM samples stayed between approximately 12 and 15 GiB (above the 8 GiB floor). Disk headroom stayed above 41 GiB. Owned test directories, caches, and temporary Node dependencies were cleaned after completion. No shared checkout cleanup was performed.
