# Task-aware compaction and retained task memory

This PR contains completion plans for PR48's four features and a runnable offline recall oracle. It changes no runtime behavior and supplies no measured model-retention result.

## Reading order

1. [00-RULES.md](00-RULES.md): factoring, authority and evidence rules.
2. [01-INDEX.md](01-INDEX.md): feature status, order, crossings and defaults.
3. [02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md): determining owners and shared contracts.
4. [03-COORDINATION.md](03-COORDINATION.md): implementation and cutover sequence.
5. [04-EVIDENCE.md](04-EVIDENCE.md): historical and current source findings.
6. [S1-TIMING.md](S1-TIMING.md), [S2-MEMORY.md](S2-MEMORY.md),
   [S3-CACHE.md](S3-CACHE.md), [S4-EVALUATION.md](S4-EVALUATION.md).

## Run the oracle

From the repository root:

```sh
python -m unittest discover -s tests -p test_compaction_retention_fixture.py -v
python tests/compaction_retention_fixture.py
python tests/compaction_retention_fixture.py --condition bounded --answers answers.json
```

The exporter supplies three synthetic history snapshots and held-out questions.
Answers map round IDs to question IDs to exact strings. The scorer reports exact,
stale and missing answers with a fixed denominator. S4 defines the native/model
runner and the revision-mass and lock-in acceptance gates.
