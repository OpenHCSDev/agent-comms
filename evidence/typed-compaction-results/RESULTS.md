# Typed compaction result closure

Source base: main 79cf99dd. Working branch: refactor/typed-compaction-results-20260928.

## Product ownership

CompactionResult is the terminal manual/worker/ACP reply family, not a durable proof,
summary-admission outcome, or native journal state. Existing journal/native outcomes
remain authoritative and unchanged. CommittedCompactionResult owns the terminal event,
committed transcript invalidation and ACP response; RefusedCompactionResult owns the
aborted event and ACP error. The worker encodes once; the attached ACP caller decodes
once. Direct callers retain the typed object throughout.

The runtime compact reply is OUR internal protocol, not an external contract.
It uses the ordinary CompactionResult derived `kind` and `commit_id` field through
unchanged FieldCodec/DeclaredFamily. External ACP PromptResponse/update semantics
are unchanged. No format reset is required: this reply is transient, not a store.
No compatibility reader, boolean wire tag or shared codec extension remains.

Deleted: raw reply dictionaries, bridge result.get/boolean effect dispatch, ACP shape
probing/fallback recasts, retired real_host fixture option, and the old launch-alias test
that required cold preparation to refuse (superseded by the current actual preparation
path). Current command tests use canonical CompactRequest/update owners, not retired
agentComms.compact/compaction metadata.

## Installed evidence

Own noneditable package: .artifacts/paired-installed/lib/python3.14/site-packages/agent_comms.
Native bundle: /home/ts/.local/share/agent-comms/native-current-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent.
Actual local HTTP provider, actual Node native process and SQLite journal; no paid calls.

- ownership.log: 15 passed, including canonical codec guard, initial (superseded) boolean projection
  checks and a declaration-only new-case extension.
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

## Final current-main checkpoint

Merged main a1d24d3a (including 312/315/317) without owned product-file overlap, then
reinstalled the complete working version noneditable.

- current-main-installed.log: 42 passed in 19.16s (13 result checks + 24 codec checks +
  four actual native manual cases + actual selected original admission exactly once).
- routing-final.log: 17 passed in 42.84s (ACP SDK/direct/attached, actual runtime socket).
- retained-native-selected.log: 1 passed in 25.24s. Actual installed launcher/native:
  warmup and reuse in retained owner; summary while that owner remains alive; file
  unchanged until commit; owner reaped after one compaction; fresh resumed owner
  receives committed summary and new input, not discarded history; exactly four
  provider calls; cleanup reaps all children. Deleted obsolete standalone-writer
  PID probe, migrated explicit canonical root/package fixture setup.
- ownership.log: 15 passed. Lint and git diff --check pass.

Retained-native intermediate receipts preserve the old fixture missing explicit root,
missing configured package, and obsolete pre-summary kill assertion. These were fixture
migrations; product native/ordinary-send logic was not changed. Final current installed
acceptance is passing. No remaining blocker for this scoped result/caller closure.
Parent owns review/integration/deployment; broader plan acceptance claims remain scoped.

## Owner review correction

Parent identified the mistaken external classification of CompactRuntimeRequest ->
RuntimeProxy -> CommsAgent. Removed the unnecessary DeclaredFamily wire_tag/decode_wire_tag
hooks, FieldCodec edits, CompactionResult boolean discriminator/overrides and commitId
projection. Removed internal wire goldens and redundant boolean validation tests. Runtime
acceptance now decodes the ordinary family and checks the typed result; ACP acceptance
still protects its real external response semantics. Earlier receipts are historical
and do not certify this corrected head; internal-kind-installed.log is its installed
current-path acceptance. Native journal/settings/session contracts are unchanged.
