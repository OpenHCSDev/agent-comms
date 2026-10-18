## Owner and cause

`PrivateEvidenceRead` owns byte acquisition, revision verification and physical-row decoding. Installed Core `012af2ffa`, `src/agent_comms/native_pi.py:691,715-724`, read the complete append into one `bytes`, retained it through `splitlines()` and `verify_snapshot()`, and therefore held the complete 173,654,666-byte session plus row copies while `NativeInputEvidenceRead` decoded it.

Live read-only census artifact: `/home/ts/wt/core-doctrine-segment/.scratch/worker-memory/census-3120903.json`. Top five by shallow reachable bytes:

1. `bytes`: 210,510,567 B
2. `str`: 38,347,162 B
3. `weakref.ReferenceType`: 17,423,040 B
4. `builtin_function_or_method`: 15,314,328 B
5. `set`: 12,520,872 B

The associated stack is `/home/ts/wt/core-doctrine-segment/.scratch/worker-memory/stack-3120903.txt`. The withdrawn live-census method and its incident are recorded in `LIVE-INJECTION-RETIRED.txt`; its scripts were deleted and will not be used again. `retainers.py` kept scanned objects only in function locals, returned, and deleted `capture`; it assigned no lock or context manager to module globals.

## Change

The removed read is the single allocation `self.stream.read(info.st_size - self.size)`, which materialized the entire session append as `appended`; the removed follow-on allocation is `appended.splitlines(keepends=True)`, which materialized references and row copies while preserving that full buffer.

`PrivateEvidenceRead._capture_append` now validates and hashes the fixed revision in 128 KiB chunks. `_decode_captured` then yields one verified physical row at a time, and the same owner performs the unchanged final full-prefix verification. There is no cache, alternate evidence reader or weaker check.

This bounds memory; it intentionally does **not** remove the full sequential verification/decode pass. `NativeInputEvidenceRead.decode_rows` still examines every physical record required to find the header and original input evidence.

## Verification

### Real builder

Command (run from `/home/ts/wt/toad-live` through its real `tools/build_runtime.py`):

```text
taskset -c 4-9 nice -n 10 <baseline>/bin/python /home/ts/wt/toad-live/tools/build_runtime.py --toad-repo /home/ts/wt/toad-live --core-repo /home/ts/wt/core-doctrine-segment --toad-ref origin/fix/declared-dependency-lock --core-ref fix/worker-evidence-memory --baseline /home/ts/.local/share/agent-comms/runtimes/toad-6f0e3211d-core-012af2ffa-native-6152254fee45b5ed-20261013T234026Z --thread change-report --display :77 --scratch /home/ts/wt/core-doctrine-segment/.scratch/worker-memory/runtime-build-with-619
```

Last output line / built runtime:

```text
/home/ts/.local/share/agent-comms/runtimes/toad-1a29b26c6-core-0e17c37eb-native-6152254fee45b5ed-20261014T001521Z
```

Full log: `/home/ts/wt/core-doctrine-segment/.scratch/worker-memory/build-with-619.log`. Toad main alone was also run, but produced no runtime because its frozen lock still omitted the five published dependencies; log: `build.log`. The successful composed build uses the exact dependency-owner PR #619 head without modifying it.

### Actual saved-session read

On the same 173,654,666-byte saved session, retaining the same 2,167 input entries:

- installed baseline: `2.87 s`, maximum RSS `410,324 KiB`
- this branch: `1.89 s`, maximum RSS `71,316 KiB`

Artifacts: `baseline-read-v2.time`, `fixed-read-v2.time`.

### Isolated worker, 20 minutes

No serving process was injected or altered. Candidate runtime above started a new isolated worker on a native fork of the same saved session. It reopened and verified that fork every five seconds; parent sampling ran every 30 seconds for 1,203.806 seconds (41 samples).

- RSS maximum: `156,213,248 B`
- RSS final: `152,530,944 B`
- USS maximum: `135,294,976 B`
- worker return code: `0`
- original source hash unchanged: `true`

Command:

```text
taskset -c 4-9 nice -n 10 /home/ts/.local/share/agent-comms/runtimes/toad-1a29b26c6-core-0e17c37eb-native-6152254fee45b5ed-20261014T001521Z/bin/python .scratch/worker-memory/measure_isolated_worker.py
```

Last output line:

```json
{"samples": 41, "rss_min": 159744, "rss_max": 156213248, "rss_last": 152530944, "uss_max": 135294976, "returncode": 0, "original_unchanged": true}
```

Artifact: `/home/ts/wt/core-doctrine-segment/.scratch/worker-memory/worker-20min.json`.

### Focused checks

- `pytest -q tests/test_native_evidence_custody.py --no-cov`: `24 passed in 5.06s`
- local `agent-comms-ratchet --root src/agent_comms --base origin/main --head HEAD`: pass
- refactor-audit AST: 309 production modules, zero omissions
