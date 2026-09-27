# PR95 local completion evidence

Implementation: `351a4930b3bf0bdebeed9178f0e5bee286a9e1c0`. The final NRA
report was rerun against this committed implementation.

Baseline: `10d5351`, with the eleven recovered dirty tracked files preserved and
completed. Publication adds the effective configuration, continued coverage,
complete native verifier, ordinary-owner support, and default activation slice.

## Ownership and replay boundary

The selected native SettingsManager owns effective compaction settings and
project trust. PiCompactionDecision decodes that decision at the RPC boundary;
the existing selected observation exchange owns bounded reads and retirement.
CompactionJournal owns reservation/exclusion. Continued private coverage is a
read-only projection of existing InputDispositions and immutable live-result SQL
columns corroborated against NativeContextProof. It creates no enrollment or
second state registry, removes no UNKNOWN rows, and grants no replay. The
existing ThreadRegistry guard still rechecks the exact owner/turn and snapshots
the goal when present. NativePackage's complete manifest replaces the smaller
hand-maintained runtime hash list.

New behavior was authored at these existing ownership boundaries. It is not a
claim of codemod equivalence. NRA's package-context before/after scans completed
in `exact_compact_global` mode with 79 detectors analyzed, zero omitted, and zero
reported findings for the selected production files. Reports are retained here.
That scan is ownership coverage, not native execution proof. The skill and current
CLI/catalog/playbook were inspected before implementation.

## Executed checks

All pytest shards used `PYTHONPATH=src`,
`/home/ts/.agent-comms/.venv/bin/python -m pytest -q -o addopts=''`, a unique
worktree-owned `--basetemp`, and a 60-second shell bound.

| Shard | Result |
| --- | --- |
| native_pi, continued_private_session, selected_summary_journal, compaction_send_admission, selected_pi_route, owner_compaction_gate | 127 passed, 6 optional native fixtures skipped |
| acp_queue_contract, acp_input_disposition, acp_owner_interrupt_followup, prompt_restart_queue, compaction_journal | 55 passed |
| owner_compaction_adaptive, owner_compaction_runtime | 14 passed |
| native_session_reopen | 10 passed (in the initial 24-case runtime/reopen shard; its unrelated detached-launcher fixture failure was corrected and the adaptive/runtime shard rerun) |
| selected_owner_compaction_integration, ordinary/configuration cases | 12 passed |
| selected_owner_compaction_integration, private/no-goal cases | 5 passed; 4 deliberately inapplicable synthetic-host/private combinations skipped |
| native selected-summary stream suite on an owned disposable copy | 42 cases passed |
| native actual services factory settings/reload/project trust/retry isolation | passed |
| scoped Ruff and git whitespace | passed |
| mypy, continued_private_session/owner_compaction_settings/selected_pi_route | passed |

Python total for the disjoint selections: 223 passed, 10 skipped. The SDK/RPC
integration runs use the default constructor policy, not an explicit opt-in.
They exercise two complete compaction/commit/reopen/original/settlement cycles,
including continued private paths, unchanged historical inputs, correction
refusal, clean decline, custom model, effective disabled settings, and no-goal
turns. The actual synthetic model stream checks that the previously committed
summary appears in subsequent native contexts. Queue and restart tests preserve
uncertain original inputs and require explicit existing admission.

Normal build: `stack/bin/prepare-pi-native`. Set `PI_COMPACTION_TEST_PACKAGE` to
`stack/.pi-native-$(sha256sum stack/pi-native.sha256 | cut -c1-16)/node_modules/@earendil-works/pi-coding-agent`.
Run `node stack/test-native-adaptive-settings.mjs` with that environment.
The 42-case suite requires an owned disposable copy with
`.pr95-disposable-test-copy` containing `owned fixture\n`, supplied as
`PI_NATIVE_PACKAGE_DIR`. `PR95_RPC_FIXTURE=1` serves only the integration host;
it does not run those 42 cases.

Normal preparation succeeded with all file pins and the whole-tree commitment.
An owned `/var/tmp/agent-comms-pi-native-*` copy passed the actual active route
verifier, and an isolated built/installed wheel verified the same full bundle.
No stock Pi package was altered. Development failures and superseded runs are
retained in `.test-artifacts/recovery-evidence/`; they are not counted as passes.

## Limits and integration

No paid/provider request, live root mutation, live restart, deployment, or CI
wait occurred. Synthetic model streams verify transport, native writer/reopen,
context retention plumbing, and exact input admission; they do not establish
real-model summary quality. Native SQL coverage tests use the actual frozen
schema and corroborated proof fixtures, not a live provider run. Parent performs
merge, installation and configured-route live validation after bus recovery.

Default activation applies to verified native ACP owners with an existing idle
selected child and matching context telemetry. Missing child/telemetry is a
clean skip. Unknown/untracked old private history remains fail-closed; it is not
auto-enrolled. This does not recover, replay or clear the original live UNKNOWN
inputs. Integration should retain S8's goal/event work while preserving the two
nullable-goal compaction guards and constructor default. Parent runtime/recovery
files and Toad were not edited.
