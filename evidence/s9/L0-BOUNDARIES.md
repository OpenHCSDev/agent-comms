# S9 L0 ownership closure

Audited against the parent REPORT.md from nra-l0b-audit-20260928.

- owner_compaction_process, compaction_child_launcher and compaction_child_watchdog
  are deleted. A12 owns all child lifetime and hard deadline behavior.
- fresh_private_session no longer has startup keyset parsing or a second ID regex;
  it uses the S10 NativeEntry capability, then matches source/selection/parent IDs.
- manual_compaction retains the separately pinned stock-Pi `/compact` operation.
  Its A12 child, strict native metadata, saved-file fsync and no-retry behavior are
  exercised against actual Pi plus loopback-only fixtures. This operation is
  distinct from canonical owner journaled compaction, as required by D21.
- manual_compaction_bridge retains the canonical-native refusal: no unjournaled
  writer may mutate that root. Obsolete descriptive markers removed; refusal
  behavior is not an old code path and is not bypassed by this refactor.
- owner_compaction_settings documentation now describes its actual ACP owner use.
  The dormant/future wording was stale; source already uses it before compaction.
- `node_modules/openai/internal/shims.mjs` is an actual pinned dependency filename.
  It remains unchanged. `agent-comms-metadata-v1\n` is the exact hash-domain prefix
  shared with the pinned native compaction writer. It remains unchanged. Neither
  is an internal identifier/comment or dual-format reader; renaming either would
  invalidate the external native commitment.
- rpc_args_for removal is assigned to Pascal's current generic-text-engine closure.
  S9 has migrated manual/adaptive callers to his canonical launch API; its
  definitions and manual-refusal authority retention remain pending integration.
  Manual stock-Pi operation and canonical-native refusal must remain distinct.

No source converters, live mutations or history replay. Runtime journal schema
reset remains a quiet parent cutover action; old evidence/history is preserved.
