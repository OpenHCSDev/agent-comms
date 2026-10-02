# Integrated S1 validation — 2026-09-27

Baseline: main e4cd10e plus published PR95 60e6cc4, normal merges acd64af/7914c9f.
All pytest shards run sequentially with xdist/coverage defaults disabled. Source imports
come from this worktree using the existing Python environment read-only. No CI wait.

| Executed surface | Result | Receipt |
| --- | --- | --- |
| Selected owner compaction, adaptive/runtime/prepare, strict reopen, admission/publication | 82 passed, 133.82 s | tests-integrated-compaction.txt |
| Native compaction, including actual ACP owner and tool rounds | 9 passed in initial native shard | native-fixture-initial-failures.txt |
| Native settlement, UNKNOWN inbox, backend/ACP interruption | 17 passed, 4 Toad skips, 67.48 s | tests-native-recheck.txt |
| Core backend, ACP, settlement and nominal extension contracts | 308 passed, 37.82 s | tests-integrated-core.txt |
| Input authority, queueing, manual bridge, activity and participant | 60 passed, 27.24 s | tests-integrated-input.txt |
| Goal lifecycle, diagnostics, routing and passive awareness | 171 passed, 36.74 s | tests-integrated-goals.txt |
| Presentation/MCP, native startup/fence, selected RPC and launcher resolution | 102 passed, 1 optional case run separately, 15.01 s | tests-integrated-presentation.txt |
| Actual native selected-summary RPC retention | 1 passed, 0.62 s | tests-selected-native-rpc.txt |

These are per-shard results with deliberate overlap, not a unique test count. The four
skips require the separate mounted Toad/Python3.14 pilot. No full-repository suite,
installed-wheel check, live runtime handshake, deployment or external provider run.

## Concrete integration corrections

- Two strict-reopen failures introduced by PR95 still produced dicts after the mechanical
  merge. Both now produce Done(reason_code="compaction_reopen_invalid") with original
  text and failure semantics; real invalid-disk/reopen tests passed under the bundle.
- All PR95 fake stream producers and reopen assertions now use nominal events.
- Native interruption had residual `e.get(...)` assertions hidden by prior opt-in skips;
  they now assert the actual declared event types and fields.
- Two old tests requested external TypeScript extensions, which the new production
  immutable launcher correctly rejects. `tests/native_event_host.*` uses the real
  prepared SDK/RPC with inline delay and existing Python CLI tool declarations.
  It verifies the bundle first, forbids non-loopback fetch, makes no package copies,
  and does not patch package bytes or weaken its production import boundary.
- Native temp roots and normal preparation honor TMPDIR. Without TMPDIR preparation
  retains its original /var/tmp default and validated path shape.

## Reproduction

Use the common environment in `validation.md`, plus:

```sh
TMPDIR="$PWD/.s1-artifacts/tmp" stack/bin/prepare-pi-native
export AC_NATIVE_STACK_BIN="$PWD/stack/bin/pi-native"
export PI_COMPACTION_TEST_PACKAGE="$PWD/stack/.pi-native-$(sha256sum stack/pi-native.sha256 | cut -c1-16)/node_modules/@earendil-works/pi-coding-agent"
```

Each shard: `timeout 165 /home/ts/.agent-comms/.venv/bin/python -m pytest -o addopts='' --basetemp="$PWD/.s1-artifacts/NAME" -q FILES`.

- Compaction: test_selected_owner_compaction_integration, test_owner_compaction_adaptive,
  test_owner_compaction_runtime, test_owner_compaction_prepare, test_native_session_reopen,
  test_selected_summary_admission, test_compaction_publication, test_compaction_send_admission.
- Native compaction: test_stack_native_compaction.
- Native other: test_stack_settlement_boundary, test_stack_inbox_output, test_stack_send_now.
- Presentation: test_mcp_relay, test_tool_diffs, test_image_inputs, test_native_startup,
  test_native_proof_journal_limit, test_maintenance_barrier, test_manual_compaction_bridge,
  test_manual_compaction_fail_closed, test_selected_pi_summary_rpc,
  test_selected_summary_exchange, test_native_owner_launcher_resolution. The optional
  actual-native RPC case also needs PI_NATIVE_PACKAGE_DIR set to the same own package.
- Core/input/goals: groups in the original validation receipt; input excludes the already
  run test_agent_events in this resume (60 versus original overlapping 68).

Normal bundle build: 194 MiB; npm test dependencies: 187 MiB. No giant package copies.
Available RAM samples stayed at 14–15 GiB, disk headroom above 41 GiB.
Ruff/Black pass all 50 S1 Python files. Shell/JS syntax checks pass.
Fresh NRA scan: exact_compact_global, 79 detectors, 0 omitted, 0 findings, 18.703 s;
no cache reuse. See nra-integrated.json. Authored async semantic moves are not a native
NRA equivalence proof. See HANDOFF.md for ownership reasoning and original paper/CLI audit.
