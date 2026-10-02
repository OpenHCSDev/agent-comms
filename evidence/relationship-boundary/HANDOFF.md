# PR138 relationship/passive-awareness complete data-boundary closure

Owner: Pascal. Persistent tree `/home/ts/wt/comms-refactor-relationships-boundary-20260928`, branch `codex/refactor-relationships-boundary-20260928`, rebased on main `61fa722` (PR168), including installed S3/PR165 and goals/PR166. Parent owns serial deployment/migration. No live writes, provider calls, helpers or model changes. Implementation complete; no known code blocker.

## Ownership and deleted mechanisms

- `RelationshipStore` now persists one `RelationshipDocument` with typed collaboration/order records. `PassiveAwarenessStore` persists one `PassiveAwarenessDocument` with typed records; no raw-row API remains.
- Deleted `_unique_edges` read-time deduplication, `_canonical_edges` raw-row decoding, store `dict[str,Any]` types and repeated encode/decode callbacks. Collaboration records own identity, orientation and updates; document owns edits and sort preferences.
- Deleted `PassiveAwarenessRecord.from_payload`, unknown-field filtering/preservation and its duplicate row validation/decoding. Source-fence and scope checks use the typed record directly.
- Deleted the `ThreadRelationships.path` and `PassiveChannelAwareness.path` forwarding aliases; current consumers/tests use the actual `store.path`. `Comms._rebase_passive_channel_scope` is the sole operations change. No ACP/TurnRunner policy change.
- Removed obsolete opposite-row projection and arbitrary-extension-preservation tests. Current tests cover explicit data migration and strict typed boundary rejection without erasing unrecognized data.
- Real rename aliases, historical incarnation checks, copyable missing-peer notes, current goal contacts, mutual editing, bounded passive frames and no-replay/no-receipt semantics are retained. No old-client coexistence gate.

## Saved data and one-way migration

`relationship_migration.py` is an explicit deployment converter; runtime modules never import it. It reads the actual version1 shape once and publishes version2 using A8 locking/atomic replacement/rollback. Normal relationship reads/writes only accept version2. One canonical edge remains per pair; duplicate original declarations (all names, notes, timestamps and orientations) become immutable history. Changed notes also retain the previous revision. Current registry rename aliases resolve only the matching incarnation; old or missing peers remain historical. Repeating migration on version2 is a no-op.

Passive awareness retains its actual version1 encoding, now decoded once by FieldCodec; no format conversion is needed. Optional malformed files remain untouched and suppress awareness; no cursor/receipt is fabricated.

Read-only real-data audit: active route `/var/tmp/agent-comms-live-20260927-wzjtqhza` has neither document. Original `/home/ts/.agent-comms` has17 relationship records,2 sort rows,85 passive rows and56 exact source witnesses, with no unknown fields or duplicate pairs. Applied migration ONLY to a copy under this worktree. Every original relationship record remains either current or in history (one renamed original is historical), both sort preferences survive, all85passive rows/56witnesses re-encode exactly. `copied-data-acceptance.json` and the proof script record this without publishing private notes or registry contents.

Parent rollout commands, from this source tree using the existing integration venv:

```sh
PYTHONPATH=src /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python \
  -m agent_comms.relationship_migration --root /path/to/root
# Apply after reviewing the preview, under parent deployment ownership:
PYTHONPATH=src /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python \
  -m agent_comms.relationship_migration --root /path/to/root --apply
```

Preview is strictly read-only, including no lock-file creation. Apply holds the existing wire and document locks; failures restore the original bytes/mode. Unknown fields/conflicting actual records fail before publication. Migrate the retained original root before serving its relationships with this code. Fresh roots create version2 directly; absent optional files stay absent during migration. No old runtime reader/dual-shape constructor is retained.

## Local acceptance

Existing integration venv, `PYTHONPATH=src`, pytest default xdist disabled with `-o addopts=''`, every shard bounded at60s.

- `core2.txt`:88 passed. Relationships, typed stores, passive ACP/InputDrain behavior, goal contacts, explicit migration, real concurrent readers/writers, real CLI preview/apply separation, no-op/reopen, data-preserving failure rollback.
- `consumers.txt`:54 passed. Tools, envelope/bus integration and LockedStore.
- `rebase166.txt`:40 passed after parent PR165/166 integration. Kept Darwin's current goal actions and registry failure-injection owner, migrated only relationship store paths. These overlap earlier shards.
- `unknown-peer.txt`:1 passed; unregistered-peer exception and no unpublished document preserved.
- Ruff on all changed source/tests and `git diff --check` pass. Earlier baseline78pass2stalefixturefail, first current77pass1removed-pathfixturefail, migration40pass1new-fixture-namefail retained as compressed logs. All corrected; baseline lock observer now accepts A8's blocking argument.

Current Toad `WireRelationshipSource` uses revision/snapshot/set_order and needs no paired caller change. It already passes typed ThreadSort. Live/provider/UI acceptance and serial installation remain parent-owned, not claimed by these tests. CI deferred.

## Exact NRA commands and limits

`NRA-S3-COMMAND.md` preserves the exact successful PR165 invocation requested by parent. `nra-command.sh` preserves this surface's invocation. Both explicitly use `--context-root src/agent_comms --no-cache --scan-budget-seconds 140 --parse-workers 1 --analysis-workers 1`, with shell `timeout 165` and compact JSON output. The internal budget prevents the default20s timeout; disabling cache prevents partial43/79-detector reuse.

Before/final scans completed79detectors,0omissions,0reported findings in the selected files with complete package context. The final scan preceded only the PR166 rebase and preserving the existing unregistered-peer exception; current seam tests cover those changes. This is architecture coverage, not native codemod equivalence. Ownership changes/scripts were authored directly; failed and successful evidence is retained.

Exact changed paths: `changed-files.txt`. All owned test/audit processes exited before cleanup. Copied private source documents, test roots, compiled bytecode and retired generated artifacts were removed; original data and all persistent worktrees remain intact.

Current-main verification: PR168 adds only integration evidence/pins; `git diff 3889a22..61fa722 -- src tests` is empty. Rebase clean, source/tests identical to the passing166seam plus the checked unregistered-peer fix. No repeated suite needed. Next assigned surface is ThreadStatus lifecycle consumer closure in a new tree, outside Darwin history/index ownership.
