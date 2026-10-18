# Handoff: ts-gaming commander, 2026-10-18

From Tristan's machine (claude-code commander and its one-on-one fork), agreed
with Tristan. Pull `main` first.

## Your work, in order

1. **Step zero of `plans/commander-native-plan.md`** (minutes, no code):
   delete branch `rts-commander-tools` and the merged commander branches
   `commander-native/phase-0`, `commander-native/phase-1`,
   `commander-native/phase-3`, `feat/p256-core-commander`,
   `feat/p181-commander-parity` after confirming each has nothing ahead of
   `main`. Run only the installed `agent-comms` commander; do not use the
   `tools/rts` copy.
2. **Phase 0 (store conversion) and Phase 1 (one owner for orders)** of the
   same plan, in parallel. Then 3 → 2 → 4 → 5 → 6/7 → 8 → 9 as its dependency
   table says. Matrix rows 23-28 are the rules and design added on 2026-10-18;
   they land in phases 3, 6 and 7. The reasons and sketches are in
   `docs/agents/commander-game.md`.
3. **The claude.ai connector** (`plans/comms-claude-connector-pr.md`), step 1
   only (prove a custom connector's tool reaches the model in claude.ai), and
   only after Tristan decides where it is hosted and whether private DMs are
   readable. Exposing Core to the internet needs his explicit go-ahead.

## What stays on Tristan's machine (do not take these)

- The build blocker (P507 parser defaults, P508 turn records without absolute
  paths, P506 registry conversion), the install, and Pi Durable (P436).
- The ~244 board rows marked "merged, install pending" that were never checked
  in the app.
- `plans/remaining-slop-plan.md` orders 2, 4, 5 and 7, and order 1 step 1 (the
  layer map only).

## Shared files and timing

- Your Phases 1 and 3 edit `src/agent_comms/commander/`, `tools.py` and
  `todos.py`. Slop order 5 (on Tristan's machine) touches `cli_commands.py`
  and is kept off your files or waits for your Phase 3. If you need a file
  outside those, say so on the bus before editing.
- Slop order 1's module moves touch every import. They wait for one quiet
  window with both commanders paused, after your Phase 1 lands.
- Do not install a Core containing #1482 (P482 turn records) on ts-gaming until
  the P506 conversion has run there too.

## Standards that apply to every slice

- An order closes only when the code is shaped right and the named run works in
  the real app. A grep closes nothing (this failed twice on 2026-10-18: P437
  broke every `agent-comms` command; Pi Durable went through five owners
  without one real turn).
- One owner per fact; delete what each change replaces in the same change; no
  compatibility readers; FieldCodec is the one codec.
- After two owners on one problem, read the failing path yourself before
  reassigning.

## Report

Post progress to Tristan on the bus in plain words: what works, what is broken,
merged / installed / seen working, next step.
