## Owner change

`PrivateEvidenceRead` remains the sole owner of native-session byte position, running SHA-256, pinned inode and filesystem revision. `VerifiedNativeEvidencePrefix` now carries those facts together.

An unchanged exact `FileRevision` performs no byte read. Ordinary growth extends the retained digest only across the appended range. Replacement, truncation and non-growth revision changes still refuse or reverify from byte zero. Initial acquisition hashes every captured byte; it repeats that full verification only if the file grows during initial decoding.

The deleted repeated full-prefix pass added one protection beyond pinned identity/revision and the running digest: detection of a same-inode rewrite of old bytes combined with apparent append growth. An owner-only Pi session is append-only; rereading 215 MB on every observation to distinguish a same-UID forged rewrite-plus-growth from an append made each stream observation linear in all history. Non-growth rewrites, replacement, truncation, malformed retained offsets, permission changes and acquisition races remain fail-closed.

## Cause

Core `503b9a093`, `src/agent_comms/native_pi.py:848-887`: every retained `rows()` called `verify_snapshot(previous_size)`, which read from byte zero before `_capture_append`; changed rows then called `_commit_snapshot`, which read from zero again.

The live caller is `src/agent_comms/input_drain.py:318-397,551`: an active turn is deliberately non-quiescent, so `WireWatch`'s at-most-one-second tick drains again. An unchanged private drain calls `CursorPublication.refresh()`, then `NativeSourceCursor._refresh_owned -> SourceCoverage.prefix -> read_historical_native_inputs`. One source-coverage operation can observe the same session reader for context and prompt corroboration. The caller needs at most one cursor advance for its drain revision; `CursorDelivery.advanced` already owns that gate. Repeated source corroborations therefore need a constant unchanged observation, not another full-file pass.

AST/refactor-audit evidence before and after: 309 production modules parsed, zero omissions. Exact declaration/reference evidence found exactly one each of `PrivateEvidenceRead`, `VerifiedNativeEvidencePrefix`, `UnverifiedNativeEvidencePrefix`, `NativeEvidenceRead`, `NativeEvidenceScope`, `_capture_append`, `_commit_snapshot`, and `verify_snapshot`; no alternate verified-prefix store or reader exists. Dynamic `source` dispatch in `SourceCoverage` was resolved manually to `CertifiedPrivateBusRead`; it does not own native file bytes.

## Measurements so far

Same 215 MB native-session fork, pinned native package `84d389a90e2228d6`, `taskset -c 4-9 nice -n 10`, source unchanged except where the isolated append case says otherwise:

- Baseline `503b9a093`, ten unchanged observations: 1.3986 s wall / 1.3915 s CPU, 2,156,125,240 bytes reread.
- Candidate, ten unchanged observations: 0.00135 s wall / 0.00134 s CPU, **0 bytes read**.
- Baseline one 75-byte append: 0.2944 s wall / 0.2928 s CPU, 431,225,198 bytes read.
- Candidate one 75-byte append: 0.000261 s wall / 0.000254 s CPU, **75 bytes read**.
- Candidate initial acquisition still read the complete 215,619,806-byte fork once.

Controlled streamed-worker pair on isolated forks of the same 215.62 MB saved-session lineage, each with 30 appends / 2,600 appended bytes / 91 observations over 30 seconds:

- Baseline Core `503b9a093`: **22.40 CPU s over 33.42 s = 67.04% of one core**; workload CPU 21.36 s; RSS max 144,920,576 B.
- Candidate final head `b0a3852ec`: **6.04 CPU s over 33.46 s = 18.05% of one core**; workload CPU 4.85 s; RSS max 145,526,784 B.
- CPU fell 73.1% process-wide and 77.3% inside the controlled stream. Both self-started workers exited 0; both original source hashes were unchanged. No provider request was made.

## Verification

- Built final-head runtime: `/home/ts/.local/share/agent-comms/runtimes/toad-8afa9b641-core-b0a3852ec-native-84d389a90e2228d6-20261014T033115Z`; builder route/import/version checks and saved-thread opening completed.
- `pytest -q -n0 tests/test_native_evidence_custody.py tests/test_native_evidence_append.py tests/test_cursor_load_recovery.py --no-cov`: **34 passed, 1 skipped in 2.02 s** at final head.
- Refactor ratchet passed against `origin/main` at final head with zero increase.
- New regression checks prove an unchanged verified revision issues zero `pread` calls, a 75-byte append hashes exactly 75 bytes, and a non-growth rewrite still closes the reader with changed-prefix evidence.
- A broader native batch remains red only in known stale test interfaces already owned by #815 (`NativePiRpcLaunch.session_dir`, removed `_trusted_package`, old `RegistryDocument.from_wire` monkeypatch, and old validation wording); collection also hits the duplicate-default fixture in `test_native_context_inspection.py`. No changed evidence path failed in that batch.
- No live worker or original saved session was modified. All measurements use newly created native forks under `/home/ts/.cache/agent-scratch/doctrine-prefix-20261014`.
