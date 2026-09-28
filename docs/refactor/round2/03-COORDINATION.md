# Round 2 coordination

**Rules:** [00-RULES.md](00-RULES.md). Round 2 runs peer to peer over agent-comms, like round 1: one agent per surface, in its own worktree and thread, talking directly to the owner of anything it crosses. No coordinator agent.

---

## Agents and when they start

| Agent | Surface | Starts |
|---|---|---|
| `refactor-r0` | R0 | step 1, now |
| the NRA owner | R1 | step 1, now |
| `refactor-s13` | S13 | step 1 (A12), then step 2 |
| `refactor-s12` | S12 | step 1 (A13), then step 2 |
| `refactor-l0a` | L0 part A | step 1, now |
| `refactor-l0b` | L0 part B | step 2; `wire_log.py` after D22 |
| the pi family's builder | S10 | step 2; V2 after A13 |
| `refactor-s9` | S9 | step 2; K3 after A13, K4 after A12 |

Each agent's dispatch message is at the end of its surface file.

---

## Status

One line in `#refactor`, only when state changes (started, PR open, guards changed, merged, blocked):

```
S12 · PR #231 open · guards 3/5 · lines −1,240 +310 · tests −48 +3 · next: native_runtime_inputs
```

Lines and tests deleted come first, because deleting is the point.

---

## Cutover

Persisted formats change, so the new version is installed at a few cutover points, **one per completed step**, not one per PR:

1. **Quiesce:** no turns and no compactions in flight; stop the owners.
2. **Install** the pinned stack: agent-comms and Toad together.
3. **Run each pending tool in `tools/cutover/` once.**
4. **Reset the runtime stores** that the step's merged surfaces declared.
5. **Relaunch the owners.** Owners launched before A12 get identities now.
6. **Delete the tools that ran,** in the next PR. A surface whose cutover tool still exists is not done.

---

## Decisions

In `docs/DECISIONS.md`; only D22 needs a fresh answer.

| ID | Decision | Answer |
|---|---|---|
| D18 | The debt ratchet and guards are the required status check on `main` | Yes |
| D19 | An exception mechanism for the ratchet | **None.** The owner can override one check on one PR by hand |
| D20 | Round 1's C0 carve | Dropped |
| D21 | Manual and owner compaction are separate operations | Yes |
| **D22** | `wire_log.py` still reads legacy protocol rows, and the wire is durable history. Rewrite the wire log once into the current protocol with a one-shot tool, or export and archive the full wire and start a fresh one? | **Owner confirmed 2026-09-28: rewrite once, preserving history in place.** See docs/DECISIONS.md; delete the pre-cutover reader after installation. |

---

## The refactor addendum

Append this to the system prompt for every round-2 surface agent, with `{{SURFACE}}` filled in. It replaces round 1's addendum.

> **Your assignment: refactoring surface `{{SURFACE}}`.** Read, in order: `docs/refactor/round2/00-RULES.md`, `01-INDEX.md`, `02-SHARED-ABSTRACTIONS.md`, then your surface file. The rules override everything else, including your own instinct to be cautious.
>
> In your surface's files, refactoring is your task, and "leave old code alone" does not apply. **Delete** legacy code, compatibility code, converters, dead code and tests of deleted code. **Never** add a fallback, an alias, a converter or a "for now" path. **Finish completely:** your surface is done when its guards pass with zero exceptions, never when most of it is done.
>
> Re-verify your surface file against current `main` first; where it is wrong, correct it in your PR and say what changed.
>
> Tests protect behaviour, not structure. Delete tests of what you deleted; replace structural tests only where a real behaviour needs protecting, with one test per family. No golden files for our own formats. Never weaken an assertion.
>
> Report lines and tests deleted and added. Post a status line to `#refactor` only when your state changes.

---

## Supersedes

This package replaces the earlier round-2 files: `10-new-debt-index.md`, `R0-R1-stop-the-inflow.md`, `S13-child-supervision.md`, `S10-pi-boundary-completion.md`, `S12-private-nk-records.md` and `S9-compaction-lifecycle.md`. Where round 1's documents allow compatibility, [00-RULES.md](00-RULES.md) lists what is revoked.
