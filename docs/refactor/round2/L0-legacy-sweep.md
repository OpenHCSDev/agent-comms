# L0: Legacy sweep

**Head audited:** `agent-comms` `main` at `15a4d00` (#225). **Rules:** [00-RULES.md](00-RULES.md), especially rules 1, 2 and 4.
**Goal:** zero legacy or compatibility code in `src/`, and every dual path ended with one path.

---

## Evidence

At `15a4d00`, 131 legacy and compatibility markers sit in 61 modules; 53 modules use the words *legacy*, *compat*, *deprecated*, *backward*, *fallback* or *shim*. Re-run this at your head before starting:

```
grep -rnE "legacy|compat|deprecat|backward|fallback|shim" src/agent_comms/
grep -rnE "\.pop\(\s*\"" src/agent_comms/        # key renames on load
```

---

## Part A: delete converters and compatibility readers

Step 1; pure deletion in files no other round-2 surface owns.

| Target | What it is | Action |
|---|---|---|
| `goals.py` | a loader renaming old keys (`status`, `block_reason`, `pause_source`) into the typed goal state | Delete the loader |
| `registry_document.py` | "Unmarked legacy stores remain readable but cannot authorize…" | Delete the legacy-store read path |
| `registry_document.py`, `registry_store.py` | `owner_epochs` loaded under its new name, and compatibility accessors (4 references) | Delete all of it |
| `message_bus.py` | the old scalar `_marker_key` (7 references) and a "compatibility entry point" for read marking | Delete; `ReadLedger` is the only read path |
| `message_bus.py` | the legacy `broadcast` route, an alias of `#all` | Delete the alias; `#all` is the only name |
| `goal_attempts.py` | in-place store migration code | Delete; the store is runtime state and is reset |
| `cohort_schema.py` | handling of "legacy" optional metadata | Delete the legacy branch; keep genuine corruption detection |
| `threads.py`, `thread_management.py` | legacy transcripts, "unknown legacy creation dates," a redirected legacy directory | Delete |
| `tools.py` | a "Configure legacy channel audience" tool and "legacy explicit declarations" | Delete the tool and the declarations; the tool list is ours, so models simply stop seeing it |
| `active_route.py`, `channel_management.py`, `goal_management.py`, `history_views.py`, `input_attempt.py` | 45 references to `legacy_*` helpers | Delete those serving old formats or paths; rename any that are merely misnamed |

**For each converter,** check the owner's install once: if no stored record still needs it, delete it; if some do, write a one-shot tool in `tools/cutover/`, run it once, then delete both the converter and the tool.

---

## Part B: end every dual path with one path

Step 2. These modules keep a legacy path running beside its replacement:

| Module | The two paths |
|---|---|
| `publisher.py` | "legacy publish," which rechecks a barrier against a fresh-root cutover, beside the fresh-root publish |
| `input_drain.py` | "the legacy ACP display cursor/ACK/steer path" and a "legacy text-only projection," beside native input |
| `supervised_cutover.py` (641 lines, 23 legacy references) | the machinery switching between the legacy root and the private root |
| `wire_log.py` | "mixed protocol rows" and legacy rows beside the current protocol |

For each dual path:

1. **Establish from evidence which path serves production today,** by tracing from the real entry points: the ACP prompt path, `comms_send`, the runtime socket.
2. **If the new path serves production,** delete the legacy path and its tests now.
3. **If the legacy path still serves production,** bring the new path to parity, switch production to it, and delete the legacy path, all in this surface. List precisely what parity requires before starting. No flag, no period of running both.
4. **When no legacy side remains, delete `supervised_cutover.py` entirely,** with its tests. Cutover machinery with nothing left to cut over is garbage.

**`wire_log.py` is the exception that needs the owner:** the wire log is durable history, which rule 2 does not allow resetting. Decision **D22**: either rewrite the wire log once into the current protocol with a one-shot tool (history stays in place; default), or export the full wire, archive the export, and start a fresh wire. Either way, the legacy row reader is deleted afterwards.

---

## Guards

A test in the suite, run by the required check (R0):

- **No identifier or comment in `src/` matches** `legacy`, `compat`, `deprecat`, `backward`, `fallback` or `shim`. Comments are included deliberately: a comment describing a legacy path means one exists. Where the word was used innocently (a "fallback" meaning a default value), rename it.
- **No module named or containing `cutover` in `src/`** once part B is done.

---

## Done when

- The guards pass with zero hits.
- Every dual path in part B has one path, and `supervised_cutover.py` is gone.
- Every cutover tool has run once and been deleted.
- Tests that exercised legacy paths or formats are deleted, not ported.
- The PR reports lines deleted and added; this surface should delete far more than it adds.

## Dispatch

Two agents, disjoint files:

> **`refactor-l0a`:** Complete L0 part A per `docs/refactor/round2/L0-legacy-sweep.md`. Read `00-RULES.md` first. Delete, do not preserve. Done when part A's targets are gone and the guard passes for your files.

> **`refactor-l0b`:** Complete L0 part B per `docs/refactor/round2/L0-legacy-sweep.md`. Read `00-RULES.md` first. For each dual path, establish which path serves production from evidence, then end with exactly one path. Wait for D22 before touching `wire_log.py`'s legacy rows. Done when part B's guards pass and `supervised_cutover.py` is deleted.

## Certified bootstrap and source coverage closure — Cicero, PR251

PR251 targets parent229 and integrates `7ba2676`. Fresh Publisher establishes the
existing checkpoint and claims marker before committing the registry guard. Native
coverage now uses only certified addressed pages and PrefixWitness; hashed-source
and whole-bus alternate readers, their caps, the separate claim initializer and
ACP public-mode execution branches are deleted. The current admission floor,
UNKNOWN, proof gaps, archived reads and registry publication checks are preserved.

Acceptance and exact file/store scope: `evidence/certified-bootstrap/HANDOFF.md`.
Fresh native tool/cursor and ACP compaction checks pass using the pinned Pi package
and a local deterministic provider. No live activation in this batch. Parent owns
D22 installation and cutover-tool deletion; Nietzsche PR248 owns older ACP fixture
closure. This checkpoint does not declare the global L0 surface complete.

## Passive-awareness caller closure — Cicero

Issue84 requires an unmentioned observer's next independently admitted turn to
include bounded source pointers while preserving NoWakeDecision. Removed the
orphan PassiveChannelAwareness JSON cursor, InputDrain instance, OwnedTurn frame
witness/veto, channel scope rebasing and both membership callers. Natural turns
now use MessageBus.awareness_prompt, reading only existing sealed Initials/Addressed
checkpoint declarations with stable incarnation lookup and the admission floor.
No new table, cursor, index, reader repair, wake, ACK or native proof. Archived
display and selected OptionalAwarenessProjection remain with their current owners.
Exclusive old tests deleted; retained relationship tests live in
tests/test_relationship_store.py. Exact acceptance is in
evidence/passive-awareness-deletion/HANDOFF.md; parent owns activation.
