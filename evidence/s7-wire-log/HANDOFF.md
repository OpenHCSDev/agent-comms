# S7 WireLog / Publisher — active implementation

Base main182 c60fe43. Tree ~/wt/comms-refactor-s7-wire-log-20260928,
branch codex/refactor-s7-wire-log-20260928. Pascal owns this surface; parent
owns Toad and activation; Darwin owns post-PR95 compaction.

## Chosen fact/file boundary

- wire_log.py / WireLog: canonical JSONL path and metadata, durable appends,
  strict private row/receipt validation, claim projection, opened-inode snapshot,
  sequence and receipt lookup, private checkpoint interaction and guarded legacy
  rewrite. It reuses the existing shared lock policy, wire codecs and checkpoint.
- publisher.py / Publisher: configuration flags, registry/catalog dependencies,
  route/mention/audience decisions, ordinary/cohort/claim/keyed publication.
  It writes through WireLog; no bus/shared-self backpointer.
- message_bus.py / MessageBus: delivery, read ledger, historical/read projection,
  existing page/route/activity indexes and caches. It composes log + publisher;
  displaced persistence/publication methods and path/flag aliases are removed.
- private_bus_checkpoint.py consumes WireLog (same store/schema/certificate).
  bus_durability.py is deleted; the shared lock invokes WireLog directly. The
  fabricated MessageBus.__new__ object is removed. Lock order/durability/UNKNOWN behavior unchanged.
- bus_publication.py already owns sideband codecs/CommittedInitial/HumanOrigin;
  reuse it. No parallel Publisher/WireLog class exists. LockedStore explicitly
  excludes the append-only log; preserve its shared canonical lock implementation.
- Current core/tests migrate to actual owners. Parent receives exact caller map.
  No production journal/provider/commit file currently needs a consumer change.

Inventory is inventory.json. No live writes, provider calls, new environments,
helper agents or CI gate. Extraction and caller migration implemented; focused acceptance in progress.

## Current focused evidence

- repair.txt: 144 passed, 1 skipped, 1 failed (checkpoint fixture still passed MessageBus).
- consumers.txt: 223 passed, including multiprocess stores, history, claims, ingress.
- coordination.txt: 128 passed (including repaired checkpoint claim/keyed case),
  1 failed: old cutover assertion expects absent bus, superseded by merged PR176
  empty checkpoint installation. Assertion updated; focused rerun pending.
- No journal/provider/commit production files edited. ACP/InputDrain/TurnRunner
  changes are only direct bus consumer migration.
- Parent handles paired Toad consumer migration from caller-map.json.
