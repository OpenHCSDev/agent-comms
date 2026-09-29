# PR354: selected native dispatch ownership (S14), ready

**281 production lines deleted; 291 added in four existing owners.** Deleted the runner's `_reserve_triage_input`, `_reserve_full_input`, `_verify_native`, `_record_triage_result`, `_record_full_result`, all calls and the duplicated cached prompt digest. `SelectedExecution` shrank by 209 class lines. No new modules, stores, schemas, codecs, state records, aliases or dispatch registries.

## Ownership and caller closure

- `NativeSendStage` shares durable reservation, current participant/input ownership and exact source-binding verification. Existing triage/full subclasses own their distinct claim reservation and settlement.
- `NativeRuntimeInput` owns the unproven reservation capability and the one CAS committing all five native context facts. Its existing schema and SQL immutability guards are unchanged.
- `NativeContextProof` owns corroboration against its isolated saved proof. This expensive history IO stays **outside** the coordinator read transaction. Live RPC events remain required; disk bytes alone grant no recovery, admission or replay authority.
- Existing `NativeIdentityCheck`, `NativeBindingCheck`, `ParticipantOwner`, `OwnerFence` and `TextDigest` are reused. No new rule-per-field family. Prelaunch and postlaunch checks share the same existing binding authority.
- Full settlement compares the existing engaged assignment's exact target with the fenced execution. Triage compares its declared deferred lifecycle value, and IGNORE keeps both SQL edges in the same transaction as its proof.
- Native RPC and proof-journal boundaries retain primitive validation; the runner's repeated decoded-type checks and eight-field tuples are gone.
- Deleted the test that monkeypatched the removed runner reader. All six owner/attempt tamper cases now mutate the actual persisted binding, check proof refusal and assert no replay. No compatibility import was restored.

Patterns: **IMPL-4/5/12** (complete existing stage behavior/shared implementation), **IDEN-1/3** (canonical ownership and record capability), **BOUND-1** (decode once). Exact authoritative archive SKILL, README/relevant catalog and NRA ownership method reread. No large NRA scan/new agents under the explicit resource restriction; this is a scoped ownership closure, not a global architecture-clean claim.

## Evidence

| Boundary | Result |
| --- | --- |
| Affected source/store failure checks | **13 passed / 5.80s**, `focused-first.log`: triage/full crash-no-replay, forged evidence, generation revocation, ambiguity, terminal failure, retained inputs, one-shot runner |
| Actual pinned native triage-to-full | **1 passed / 4.93s**, `native-triage-first.log`: three local HTTP responses; one triage input, one full input in the same saved session, four coding tools, exact proof/binding, one reply, one-shot enforcement |
| **Noneditable installed** final production `02c0e120` | **2 passed / 16.38s**, `installed-native-first.log`: same triage/full case plus full channel reply, automatic sender IGNORE, real owner sockets/ACP cursor feedback, no repeated/recursive delivery. Five local HTTP responses total |
| Affected source-binding/raw-admission checks | **16 passed / 5.76s**, `affected-binding.log`: pre-send drift, missing/uncertain binding, actual persisted identity tampering, digest mismatch, ownership changes and launch failure |
| Persisted tamper cases after preserving explicit retry assertions | **6 passed / 2.44s**, `persisted-no-replay.log`; these six overlap the preceding 16, not additional distinct coverage |
| Scoped ratchet | **No increases**. Type checks −6, long chains −5, chain terms −45, foreign absence probes −9, god-class excess −209. `ratchet-summary.json`, complete compressed receipt |
| Lint | Passed, `lint.log` |

Installed helper paths and byte equality for all four changed production modules are recorded in `installed-imports.json`; all loaded core modules came from the wheel candidate, with no source `PYTHONPATH`. The candidate includes normal merges of current main/350/351, not reconstructed code. Later changes only close tests/evidence.

### Retained failed attempt

`native-wire-first.log` records the earlier source-harness failure at ACP attachment (missing owner socket), before selected dispatch. It is not reported as passing. That run used relative `PYTHONPATH=src` while detached owners change cwd to their worktree. `source-launch-import.json` proves the same interpreter/cwd combination imports the **old installed** core rather than this checkout. The exact worker stderr was removed by fixture cleanup, so the import mismatch is established, while attribution of that socket failure to it remains an inference. The final noneditable candidate ran the unchanged channel case successfully, including actual child/ACP boundaries.

## Integration and cleanup

Parent owns live installation and final painted UI observation. No live root reset, owner restart or original input replay was performed. No paid provider calls or new agents.

Owned derivative installation removed only after checking process references (`cleanup.json`). Source, branch, wheel, receipts and global canonical native d396 are preserved. Fixture `finally` blocks stopped both actual owners and their local HTTP server.

PR351/350 are merged; this branch normally merges main through `95a792db`. No edits to Boyle's admission/response/shared-identity files, Carver startup, or Tesla UI. Parent can review/merge this checkpoint without a CI hold or another unchanged matrix.
