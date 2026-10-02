## What changed

The original helper79cb failure combined an idle pre-begin thread with a new turn lease. Native AgentInfo attachment then correctly failed the exact registry turn check.

`AgentActivity.begin_turn` now returns the existing `RegistryOwner` from its atomic lease transaction. `OwnedTurn` keeps that one owner and replaces it with the result of the existing native-source attachment transaction. `TurnProgress` borrows the turn and derives thread, lease, routing, input source and checkpoint. Five separately stored semantic fields and the callback reconstruction of RegistryOwner are removed.

All three production callers consume the returned owner: ordinary input/native callbacks, manual compaction preparation and relay settlement. All assigned lexical helper/test callers were migrated; ignored results remain ignored. No snapshot refresh, weaker check, new class/store, native artifact or durable format change.

## Source closure

Normal merges include main356068c7 plus the published540 original retained-source qualifier receipt fd3812a6. Production delta against main: **6 files,61 added/30 deleted lines**. RegistryTurnRule is unchanged.

Existing NRA/refactor-audit parser evidence covers source, tests and tools before/after: src311/tests358/tools53 after, zero parse omissions.68 lexical begin calls:6 consume the owner,22 derive the lease,39 ignore the return,1 checks truthiness. RegistryOwner/OwnedTurn/TurnProgress/RegistryTurnRule each have exactly one production declaration. AST establishes lexical consumers and syntactic inheritance; dynamic dispatch is not claimed.

Evidence: `evidence/turn-lease-native-source-20261002/{SOURCE.md,before-ast.json,after-ast.json,all-begin-consumers-after.json,source-closure-receipt.json}`.

## Final validation and remaining acceptance

One source sanity batch: **17 passed/3 stale-fixture failures in2.50s**, with raw failures retained. The real local Comms/OwnedTurn path admits idle ownership, begins, opens callback resources and attaches native AgentInfo after a phase change. Reused turn IDs and replaced admissions refuse stale source publication without registry writes. This is source-only, not installed native readiness.

Failures are existing old API callers: QueuedInputContext.capture argument count and two singular selected-stage.assignment fixture accesses after the plural assignments owner change. They are recorded, not suppressed; no unrelated production repair or broad rerun.

**Installed acceptance passed:** ONE configured saved SDK-fork native/ACP journey,51.9857s on the source-equivalent7dd package. Real Toad headless editor→ACP→native configured Sol/OFF: one new input, exact native reply, same original turn/admission/generation through source attachment and canonical idle. The original41MB history/settings remain unchanged. Owned harness/native processes are absent, with no private-root borrowers found; inaccessible unrelated processes are explicitly unclassified. This is installed headless application/protocol acceptance, not physical st/public activation or a latency-fix claim.

Original authored receipt0cee2ee is copied byte-for-byte and hash verified under `evidence/turn-lease-native-source-20261002/installed/`. Receipt SHAa632f2ddfa2f518865a5eaaaecf367046a7df68a45f19a91ee24b8672fcfe816. No second provider run.

Original79cb UNKNOWN and540 original NotSentInput/source-preparation qualifiers are preserved. No original replay or public/default mutation.
