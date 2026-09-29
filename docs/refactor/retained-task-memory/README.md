# Complete task-aware compaction and retained task memory

Planning and provider-free evaluation scaffold against `OpenHCSDev/agent-comms`
main `697bba42f5f03e169ff8eae9490090cbd0d0b89e`, 2026-09-29.
Current owner: `comms428`, at Tristan's direct request. Initial draft publication
was by `openhcs-pr159-viewer-bind-owner`; followthrough uses `/home/ts/wt/comms428`.

The original PR48 proposal included task-aware timing, exact task memory,
cache-preserving summaries, and comparative retention evaluation. Live journaled
compaction is not completion of those promises. This package records the gap,
identifies existing owners to extend, and supplies a runnable recall oracle.
It adds no runtime route, installed-package change, or provider request.

## Reading order

1. [00-RULES.md](00-RULES.md): correct factoring and data protection.
2. [01-INDEX.md](01-INDEX.md): evidence, feature status, order and crossings.
3. [02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md): existing owners, not another pipeline.
4. [03-COORDINATION.md](03-COORDINATION.md): dispatch boundaries and product defaults.
5. [04-EVIDENCE.md](04-EVIDENCE.md): PR history, source receipts, limits, reproduction.
6. [05-AUDIT-REVIEW.md](05-AUDIT-REVIEW.md): complete contextual scaffold scan,
   counterevidence and the required source-derived scoring repair.
7. [S1-TIMING.md](S1-TIMING.md), [S2-MEMORY.md](S2-MEMORY.md),
   [S3-CACHE.md](S3-CACHE.md), [S4-EVALUATION.md](S4-EVALUATION.md).

The surface receipts are provisional design proposals, not admitted NRA migration
proofs. They require refreshed contextual scans and behavioral evidence before
production changes. JavaScript native code is outside NRA's Python proof scope.

## Runnable infrastructure

From the repository root, with Python 3.14 (the stack's native-test version):

```sh
python -m unittest discover -s tests -p test_compaction_retention_fixture.py -v
python tests/compaction_retention_fixture.py
python tests/compaction_retention_fixture.py --condition bounded --answers /persistent/path/answers.json
```

The exporter emits three synthetic history snapshots and held-out questions.
Answers JSON maps round IDs to question IDs to exact strings. The scorer measures
exact recall, stale facts, and missing answers with a fixed denominator. No run
here calls a model, compacts a session, or establishes a retention-quality result.
See S4 for actual native/provider runs and reporting that remain to be done.
