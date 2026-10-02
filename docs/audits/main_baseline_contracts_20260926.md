# Main baseline contract cleanup — 2026-09-26

Base: `3e1813eb8c205b2276b44827adbe09cce3c83f3f` (PR96 merged).
Historical baseline: `2b38c5d`; its 15 failures are preserved in
`/home/ts/.agent-comms/.test-tmp/mcp-main-baseline.log` and
`/home/ts/.agent-comms/.test-tmp/mcp-main-merge-root.log`.
Worktree: `/home/ts/wt/comms-main-baseline-contracts`.

Scope: test contracts only. No production behavior, receipt authority, installed
runtime, or live sessions changed. Diagnostic `076f025124d1450482c9a3462d774539`
and UNKNOWN input 7590 were inspected as historical evidence, not replayed.
Prior September 24 macOS patches remain untouched under `/dev/shm`.

## Reproduction and classification

All 15 historical failures reproduced on the exact base in 16.25 seconds.
Log: `.test-tmp/baseline-15.log`. This first targeted run inherited the repository's
coverage gate and failed that gate too; subsequent focused runs explicitly use
`--no-cov` and make no coverage claim. An earlier broad run with default xdist /
coverage exceeded a 150-second command limit and is not counted as completed.

### Eleven strict terminal-event expectations

`tests/test_backend.py::TestRpcParsing::`:

- `test_large_end_of_turn_record_does_not_fail_completed_reply[False]`
- `test_large_end_of_turn_record_does_not_fail_completed_reply[True]`
- `test_positive_final_usage_survives_empty_stats_and_final_stop_gate`
- `test_outbound_session_mutation_rejected_before_write_but_a_continues[new_session]`
- `test_outbound_session_mutation_rejected_before_write_but_a_continues[switch_session]`
- `test_outbound_session_mutation_rejected_before_write_but_a_continues[fork]`
- `test_outbound_session_mutation_rejected_before_write_but_a_continues[clone]`
- `test_retry_recovery_requires_substantive_model_progress`
- `test_routine_compaction_emits_no_false_recovery_states`
- `test_prestart_compaction_outlives_prompt_start_wait`
- `test_watchdog_never_times_out_an_active_tool`

These omitted the existing `diagnostic: {exit_code: 0}` field. Expectations now
compare the complete event, including that exact diagnostic, without filtering
unknown fields. Existing no-replay, mutation-rejection and recovery assertions
remain intact.

### One ACP forwarding expectation

`tests/test_acp.py::TestAgentTurnForwarding::test_turn_forwards_tool_calls_and_thinking`

The owner now emits two `inputDeliveryChanged` metadata invalidations. Assert
both complete metadata payloads and empty text before separating them from model
output. Preserve exact model/tool event order and explicitly assert committed
transcript invalidation precedes turn settlement. UNKNOWN and inputStarted
assertions are retained.

### Two channel UNKNOWN visibility expectations

`tests/test_acp_channel_disposition.py::test_channel_queued_before_revocation_remains_visible_unknown[goal]`
and `[reopen]` expected no-longer-queued inputs in the current awaiting list.
Main commit `7337e44830ab86b4f689595ff330602603695442` deliberately separates
live owner queue notices from earlier UNKNOWN notices. The existing
`test_input_delivery_current.py` specifies that separation.

The corrected test explicitly checks current list empty, historicalCount=1,
undismissed historical details with exact sequence/target/UNKNOWN, unchanged
ledger and cursor, and no redelivery or backend launch. The stopped-owner case
still checks compatibility current visibility. This is a projection contract
change, not permission to hide, dismiss, resolve, or replay uncertain inputs.

### One ACK-only fixture deadlock

`tests/test_acp_input_disposition.py::test_started_then_ack_only_direct_survives_reopen_without_replay`

An instrumented copy (`.test-tmp/test_ack_probe.py`, `.test-tmp/ack-probe.log`)
confirmed the child received get_state, original prompt and steer. Ledger state
was first STARTED, second UNKNOWN. The fixture ACKed steering but sent only the
original final/settled, then waited for stats. Persistent backend correctly
ignores that settlement while a forwarded input lacks its own start; therefore
neither side could advance. The old fixture described one-shot settlement, not
the persistent contract.

The child now exits after ACK-only steering to exercise loss before proof.
Assertions retain STARTED vs UNKNOWN on reopen and no queued replay; a launch
counter additionally requires exactly one child launch. No backend timeout or
settlement fence is weakened.

## Validation

Provider-free commands, from the worktree:

```
env -i HOME=/home/ts PATH="$PATH" PYTHONPATH="$PWD/src" \
  /home/ts/.agent-comms/.venv/bin/python -m pytest -n 0 --no-cov -q \
  tests/test_backend.py tests/test_acp.py \
  tests/test_acp_channel_disposition.py tests/test_acp_input_disposition.py \
  tests/test_input_delivery_current.py tests/test_input_delivery_history.py
```

Final result: **314 passed in 57.33s**, `.test-tmp/final-six-modules.log`.
All 15 original failures plus delivery-history tests passed in the preceding
focused run (19 passed). Ruff check on all four changed test modules passed;
`git diff --check` passed. These are Linux/Python 3.11 results, not macOS,
Windows, live-provider, or whole-repository green claims. Final SHA and any
additional suite results are provided in the handoff/PR to avoid a self-hash.
