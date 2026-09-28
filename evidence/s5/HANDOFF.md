# S5 identity/counter ownership — implementation checkpoint

Owner: this worker, persistent `~/wt/comms-refactor-s5-identity-20260928`.
Parent owns integration/deployment; Darwin owns ACP InputDrain/compaction policy.

## Implemented

- A7 `ThreadIncarnation` identifies historical name/creation provenance;
  `OwnerIdentity` adds the process generation; `TurnIdentity` adds its turn.
  Unlike the provisional plan, DM identity deliberately excludes process ownership.
- `GenerationCounter` owns monotonic allocation/tombstones. Registry uses it for
  separate owner and admission domains. Turn counter advances only at turn start;
  metadata and turn completion do not rotate owners. Idle heartbeat is presence.
- ActiveTurn carries its owner attestation; `_turn_epochs` is removed. Registration
  cannot restore authority to a saved revoked turn or forge the turn counter.
- Begin/finish fences store a TurnIdentity. ACP's TurnClaimFence import remains an
  alias to TurnLeaseFence; legacy scalar projections/constructor remain supported.
- Launch pipe digest binds typed OwnerIdentity. Compaction readers consume explicit
  owner generations; normal runtime distinctly names admission vs participant generations.
- DM display and read conversations consume ThreadIncarnation. Old S4 pair-shaped
  persisted conversation keys decode at their boundary. Old registry owner/turn
  epoch JSON reads migrate on write; new writes have only the new generation keys.
- Coordination participant generation remains deliberately independent: explicit
  assignment/retry domain in SQLite, not the process/admission counter.
- Audited src/tests/stack and local Toad source/worktrees: only the old resource_claims
  test imported that module. Removed both; envelope claims remain untouched.

## Local evidence so far

- Baseline: 142 passed, 1 skipped.
- Identity/registry/DM/restoration/goal shard: 196 passed, 1 skipped.
- Initial consumer shard: 138 passed, 37 skipped, 1 stale test assumption about
  metadata rotating launch authority. Updated to perform an actual owner replacement.
- Runtime/restart shard exposed saved-turn equality assertions (attestation now
  intentionally stripped), admission keyword fixture migration, and the known
  parent native_prompt_binding observer fixture. Update tests and merge latest main.
- Real restart child needs absolute PYTHONPATH because it starts in a different cwd;
  rerun with that environment. Provider tests skipped without package selection.
- Failed logs retained in `.artifacts/s5`. No providers, CI, install or live changes.

## Next milestone

Finish the consumer shard after main's fixture update, actual local startup/restart
and persisted compatibility tests, final NRA scan, draft fork PR. No production
blocker identified. Authored behavior is validated by execution, not claimed as
an NRA native equivalence proof. Old broad terminology-only attention renames are
not part of this identity change; no claims/tool policy is modified.
