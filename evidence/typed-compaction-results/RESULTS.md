# Typed compaction result closure

Source base: main 79cf99dd. Working branch: refactor/typed-compaction-results-20260928.

## Product ownership

CompactionResult is the terminal manual/worker/ACP reply family, not a durable proof,
summary-admission outcome, or native journal state. Existing journal/native outcomes
remain authoritative and unchanged. CommittedCompactionResult owns the terminal event,
committed transcript invalidation and ACP response; RefusedCompactionResult owns the
aborted event and ACP error. The worker encodes once; the attached ACP caller decodes
once. Direct callers retain the typed object throughout.

FieldCodec remains the only codec. DeclaredFamily owns external discriminator hooks;
ordinary string families retain their existing projection. Boolean `ok` in the existing
socket response is preserved exactly, including commitId spelling. No schema/history
migration, compatibility adapter, parallel member roster, or automatic retry is added.

Deleted: raw reply dictionaries, bridge result.get/boolean effect dispatch, ACP shape
probing/fallback recasts, retired real_host fixture option, and the old launch-alias test
that required cold preparation to refuse (superseded by the current actual preparation
path). Current command tests use canonical CompactRequest/update owners, not retired
agentComms.compact/compaction metadata.

## Installed evidence

Own noneditable package: .artifacts/paired-installed/lib/python3.14/site-packages/agent_comms.
Native bundle: /home/ts/.local/share/agent-comms/native-current-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent.
Actual local HTTP provider, actual Node native process and SQLite journal; no paid calls.

- ownership.log: 15 passed, including canonical codec guard, strict external field
  validation, no bool/int coercion and a declaration-only new-case extension.
- native-manual-current.log: 4 passed in 13.11s; real selected summary commits without
  inventing original input, known refusal recovery, reserved/unknown attempts preserved.
- installed.log: 57 passed / 6 failed in 123.32s. Passing tests include selected original
  admission exactly once and real native manual dependency settlement/no replay. Failures
  were an obsolete cold-start fixture, an accidental raw socket assertion migration,
  and four retired fixture keyword callers; all corrected/deleted in this batch.
- routing-final.log and retained-native.log: final current-path reruns recorded separately.

The initial run omitted PI_COMPACTION_TEST_PACKAGE and failed fixture setup; another
selector used the wrong class name. Neither is represented as acceptance. No live tree,
route, launcher, native discovery or ordinary-send implementation was edited. CI deferred.

S7 scale evidence is parent-corrected using existing 285/304 receipts; no benchmark rerun.
