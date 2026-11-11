# Module ownership: finishing the package move and killing the slop it exposes

> Status: **P0 evidence under correction; P1 incomplete.** Phases are encoded in
> `plan_phases.py`. Four P1 tool relocations were merged before P0's evidence was
> established correctly; the fifth is preserved, unmerged, in PR #4327. No further
> moves start until the P0 repair is accepted. The original design baseline below
> was measured on `eb664473a`; the corrected P0 source census uses `51d17a5cb`.

This plan exists because `PACKAGE-STRUCTURE-PLAN.md` is marked **Done** while 69% of
the production code is still flat. That is not a complaint about the people who
finished it: their five phases each carried a `PathsAbsent` predicate over a named
file list, and those predicates are all true. What they do not assert — the plan
says so itself — is *destination ownership, behavioural parity, or complete
consumer closure*. This plan is the rest of that sentence.

---

## 1. Baseline (measured, reproducible)

Commands used, all cheap (the full 685-file detector scan is **14.9 s**: parse 4.1 s,
analysis 10.8 s; the rich economics/plans mode is the one that blocks, and this plan
does not need it):

```
nominal-refactor-advisor --json --no-auto-context-root src          # 533 findings
python3  # module/line census over src/agent_comms
```

### Where the module surface stands

| Surface | Modules | Lines | Share |
|---|---:|---:|---:|
| **flat `src/agent_comms/*.py`** | **410** | **119,024** | **69%** |
| `cli_bridges` | 28 | 8,755 | 5% |
| `storage` | 7 | 7,043 | 4% |
| `installer` | 37 | 6,477 | 4% |
| `coordination` | 25 | 4,958 | 3% |
| `nominal` | 6 | 4,684 | 3% |
| `team` | 15 | 4,525 | 3% |
| `registry` | 5 | 3,590 | 2% |
| `ui_model` | 10 | 3,524 | 2% |
| `execution` | 5 | 2,885 | 2% |
| `cli_verbs` | 82 | 2,640 | 2% |
| `transport` | 7 | 1,549 | 1% |
| `planning` | 2 | 680 | <1% |
| (everything else) | 33 | 2,426 | 1% |
| **total** | **672** | **172,760** | |

Largest flat modules, i.e. the mass this plan has to place:

```
7134  todos.py            1518  turn_context.py    1396  repo.py
2625  child_process.py    1470  phase_evidence.py  1362  messages.py
1795  tools.py            1428  integration.py     1331  rule_deal.py
1555  standing_rules.py   1402  pi_payloads.py     1266  read_basis.py
```

69 flat modules are over 500 lines; 5 are over 1,500.

### What the finished plan actually proved

`src/agent_comms/plan_phases.py:1053-1109`, verbatim:

| Phase | `PathsAbsent(...)` over |
|---|---|
| `PackageDeclarationsPhase` | `field_codec.py`, `declared_family.py`, `json_schema.py`, `sealed.py` |
| `PackageStoragePhase` | `store_files.py`, `locked_store.py`, `private_path.py`, `maintenance_barrier.py`, `typed_table.py` |
| `PackageCoordinationPhase` | `coordinator.py`, `coordinator_vocabulary.py`, `coordination_tables`, 10 × `coordination_*.py` |
| `PackageExecutionPhase` | `input_drain.py`, `session_lifecycle.py`, `owner_lifecycle.py`, `owned_turn.py` |
| `PackageConsumerClosurePhase` | `registration.py`, `registry_store.py`, `registry_document.py`, `threads.py`, `goal_waits.py` |

All true. The plan's own warning: *"Their deletion predicates track removal of the
flat paths; they do not prove destination ownership, behavioral parity or complete
consumer closure."*

### Where the slop is

Complete scan, 685 files, 110 detectors, 0 omitted, `complete: true`, 14.9 s,
**533 findings across 25 patterns**:

| # | Pattern | Findings |
|---|---|---:|
| P37 | Restated Identity | 74 |
| P39 | Missing Mixin | 54 |
| P40 | Constant Subtype Answer | 47 |
| P49 | Own-Class Type Test | 46 |
| P47 | Name-Only Subclass | 44 |
| P38 | Duplicate Class | 42 |
| P1 | Nominal Boundary Over Sentinel Simulation | 40 |
| P43 | Zero-Or-One Tuple | 35 |
| P44 | Threaded Absence | 33 |
| P36 | Forwarding Facade | 30 |
| P33 | Foreign Absence Probe | 24 |
| P23/P25/P24/P26 | source-backed leads (dispatch, module expression, literal roster, foreign type switch) | 38 |

315 of 533 sit in the top five, and four of those five are one disease: **the
family exists but the behaviour is not on it** — a leaf answers by constant, a
subclass shares only a name, a consumer type-tests its own class instead of calling
a method, a capability is re-implemented instead of mixed in (paper1 §1: every pair
the declared lattice does not derive is restated by a consumer; paper1 Prop. 4.2 /
Cor. 4.12 put the derived form at zero outside pairs).

---

## 2. The relation this plan admits

Per the papers and `skills/nra-refactoring`, a move is only correct when the
*required* relation holds — not when the file is gone.

**R\* (declaration ownership).** For each eligible top-level `ClassDef`,
`FunctionDef` and module-level constant in the flat surface, `R*` names the one
package that owns it. `R*` is **provisional** until the ownership trace in §5 has
been done for that batch: declaration, implementations, consumers, projections.

**R\* (implementation pairs).** Where a capability is repeated, `R*` names the
provider (the mixin / ancestor / family member) and the consumers that must derive
from it. A pair is *supplied* when the consumer inherits the provider, *restated*
when a consumer copies, forwards, or re-decides it.

**F\* (forbidden).** A consumer outside the owning package importing a moved
module's old path; a flat module surviving its own batch; a new `if kind == ...`,
`getattr(x, "y", default)`, or hand roster introduced by any batch in this plan.

### Evidence every phase must carry

`PathsAbsent` alone is what went wrong. Each phase in this plan carries
`AllEvidence` of at least three independent assertions, using the existing
vocabulary in `src/agent_comms/phase_evidence.py`:

1. **`PathsAbsent((...))`** — the old flat paths are gone.
2. **`PatternAbsent(r"from agent_comms\.<old> import|agent_comms\.<old>\.", ("src", "tools", "stack"))`**
   — no consumer anywhere still names the old path. This is the assertion the last
   plan lacked, and the one that a rename inside the same change satisfies honestly.
3. **`PatternAbsent(<pattern regex>, <paths>)`** where the phase claims a slop
   reduction — the census for that pattern must not grow across the phase.
4. **Behaviour:** the owning test family for the moved modules passes unchanged,
   plus `agent-comms install` on a scratch bus. Named per phase.

`NoExactCopies` / `NoGodFunction` / `NoAbsenceProbes` / `ClassFlagsAbsent` are used
where a phase targets those shapes directly rather than a move.

---

## 3. Destinations (extending the existing table)

`PACKAGE-STRUCTURE-PLAN.md` names nine destinations. Seven exist. Two do not, and
one is a stub holding 680 lines against the 7,134-line `todos.py` it is supposed to
own:

| Destination | Today | What this plan moves into it |
|---|---|---|
| `messaging/` | **does not exist** | `messages.py` (1,362), `read_basis.py` (1,266), `message_*.py`, target/channel families, delivery cursors, `bus*.py` history readers |
| `projects/` | **does not exist** | `repo.py` (1,396), `checkout*.py`, `worktree*.py`, project-side storage-rule types, `integration.py` (1,428) if it is project integration |
| `planning/` | 2 files, 680 lines | `todos.py` (7,134), goal/wait families still flat, `phase_evidence.py` (1,470), plan/phase records, `rule_deal.py` (1,331) if it is deal evidence |
| `execution/` | exists, 2,885 | `child_process.py` (2,625), `turn_context.py` (1,518), `native_*.py` runtime-side owners |
| `transport/` | exists, 1,549 | `pi_payloads.py` (1,402), the `ToolRequest` split per #4214 |
| `registry/` | exists, 3,590 | remaining `thread*`/`registration`/`alias` modules still flat |
| `storage/` | exists, 7,043 | remaining `store_*`/`lock*`/`private_*` modules still flat |

`comms.py` stays the composition root; `cli_support`, `commander`, `installer`,
`context_segments`, `team` and the skills distribution stay where they are.
`todos.py`, `tools.py` and `standing_rules.py` are *not* moved wholesale: each is
split at its own owner boundary first (§4, phase P1) — moving a 7,000-line mirror
into a package just gives the mirror a nicer address (AGENT-6: relocation is not
factoring).

---

## 4. Phases

Ordered by dependency, not by size. Each phase is a bounded batch with its own
PR(s) and its own evidence. Nothing here starts before its predecessor's evidence
is on main; the census is re-run once per phase, not per edit.

### P0 — Make the map, not the moves (no source change)

- One complete NRA scan of `src` **with** `--json --json-payload full
  --raw-findings` at the phase head; store the JSON under the plan's evidence path.
  This is the map for P1..P5 and the baseline for the slop deltas.
- Record the ownership relation `R*` for the **first batch only** (P1's target), with
  the declaration → implementations → consumers trace. One file, no invented rows;
  unresolved rows are `OPEN` with the reason.
- Add the plan and its phases to `plan_phases.py`, with the four-part evidence from
  §2. No phase may be declared done by `PathsAbsent` alone.
- **Evidence:** the scan JSON committed; `R*` document committed; phases present
  with `AllEvidence`. **Behaviour:** none (no source change).

#### P0 evidence correction

At source commit `51d17a5cb1db130bbeb707e5448b5daa92670c54`, tracked Python
files under `src/agent_comms` contain **673 modules and 172,914 lines** (LF bytes,
matching `wc -l`): 410 flat modules / 119,158 lines, plus 263 packaged modules /
53,756 lines. The earlier claim combined the recursive module count with only the
flat line count. The retained census is
`docs/plan-evidence/module-map-counts-51d17a5cb.json`.

The earlier `nra-p0-51d17a5cb.json` records compact semantic-boundary output from a
scan of `.`. Its 584 findings are **not** the required full/raw `src` baseline.
It remains historical evidence, not the accepted baseline for phase deltas.
A subsequent full/raw attempt exited 124 with `complete: false` after its
280-second budget; the checkout also changed during a restart. A replay on a
read-only snapshot of the exact committed source also exited 124, exhausting
560 seconds in `contextual_global_prepare:repeated_concrete_type_case_analysis`.
Neither establishes P0 completion. NRA hard-exits at the deadline, so ordinary
cProfile finalization did not save its profile. A separate 30-second launch-time
attribution probe saves the profile before that exit; it is diagnostic evidence,
not a replacement baseline or a successful scan.

`nra-p0-full-51d17a5cb.json` is the full/raw `src` scan that completed. NRA `main`
at `8989beb71eb5` runs every one of its 110 detectors on its exact compact-global
path, so the scan never falls back to the stage that exhausted both deadlines:
`complete: true`, 0 detectors omitted, 686 source files, 521 raw findings over 25
patterns, 89 s wall time with the analysis cache disabled. It ran on a read-only
`git archive` of `51d17a5cb` extracted under a scratch root, which prefixes every
path in the file; strip everything before `src/`. Command, from that root:
`nominal-refactor-advisor --json --raw-findings --no-auto-context-root --no-cache
--scan-budget-seconds 2300 src`.

`ModuleMapPhase` requires the source census, full/raw scan, static import map and
first-batch ownership trace at their recorded Git blob contents. Its manifest is
written only after the artifacts are checked; absent artifacts, absent entries
and changed contents leave the phase incomplete. A sentence in this document
cannot substitute for those records. The import-map scope and remaining `OPEN`
cases are recorded in `docs/plan-evidence/R-star-p1.md`.

### P1 — Split the two mirrors that keep the root fat

`todos.py` (7,134), `tools.py` (1,795) and `standing_rules.py` (1,555) are not
modules, they are *several* owners sharing a file. Splitting them is what makes
`planning/` real, and it removes the standing temptation to `git mv` a mirror.

- **Relation:** declarations that answer *goal/order/plan* → `planning/`;
  declarations that answer *tool declaration/transport* → `transport/` (this is
  #4214's `ToolRequest` split, done by the same owner); declarations that answer
  *team standing rule* → `team/`.
- **Operations:** `CreateFileOperation`, `ExtractAuthorityOperation`,
  `DeclareAuthorityOperation`, `EnsureImportOperation`, `RemoveImportNamesOperation`,
  `PatchTargetOperation` for the residue the declared operations do not cover.
- **Evidence:** `PathsAbsent(todos.py, tools.py, standing_rules.py)` +
  `PatternAbsent("from agent_comms.todos import")` across `src tools stack` +
  census deltas for P36/P38 + the goal/tool/rule test families.
- **Behaviour:** `comms_goal`/order/plan suites, tool declaration suites, one real
  `agent-comms install` on a scratch bus.
- **Risk (OPEN):** `todos.py` is imported by 100+ modules; the import-graph closure
  must come from the P0 scan, not from grep.
- **Prereq from another owner:** land or close #4214 first — it plans the
  `ToolRequest` half and must not be duplicated.

### P2 — `projects/` exists: the repository and checkout owners

Move the repo/checkout/worktree family out of the flat surface into `projects/`.

**On main (2026-11-08):** `projects/` holds `repo`, `checkouts`, `git_errors`,
`repository_sources`, `storage_projection` and `storage_rules` (claude-p-pkg's
ProjectOwnersPhase). Still flat: `checkout_publication.py`, `integration.py` and
`repository_name.py`; each moves only if its trace says it answers a `projects/` fact.

- **Operations:** `CreateFileOperation` + `PatchTargetOperation` for module bodies;
  `EnsureImportOperation`/`RemoveImportNamesOperation` for consumer closure.
- **Evidence:** `PathsAbsent(...)` + `PatternAbsent("agent_comms.repo\b")` +
  checkout/repo suites + a real checkout operation on a scratch root.
- **Behaviour:** `agent-comms checkout add/remove`, the repo test family, one
  install.

### P3 — `messaging/` exists: the conversation owners

Move `messages.py`, `read_basis.py`, message reference/target/channel families and
delivery cursors into `messaging/`. This is the phase that closes
`PackageConsumerClosurePhase`'s "messaging" claim, which was never true.

- **Operations:** file/import ops; `CollapseRedundantClassAuthorityOperation` where a
  message family has two authorities today (P38/P37 findings in this area).
- **Evidence:** `PathsAbsent(...)` + `PatternAbsent("agent_comms.messages\b")` + the
  bus/delivery/read-ledger suites + one install; census delta for P37.
- **Behaviour:** bus send/read round-trip against a scratch bus, plus Toad's reader
  path exercised by the toad test env.

### P4 — `execution/` takes the runtime mass

`child_process.py` (2,625) and `turn_context.py` (1,518) are the two biggest flat
modules whose behaviour is already an `execution/` concern. `native_*.py` follows
where it answers lifetime/turn facts rather than transport framing.

- **Operations:** `CreateFileOperation`, `PatchTargetOperation`,
  `CollapseIntermediateClassAuthorityOperation` where a relay class exists only to
  forward (P36 Forwarding Facade, 30 findings repo-wide).
- **Evidence:** `PathsAbsent(...)` + `PatternAbsent(...)` + process/lifecycle suites
  + one install; census delta for P36.
- **Behaviour:** hook/watch/child-process suites, plus a real owner restart.

### P5 — The slop the moves expose, pattern by pattern

Only after the surface is placed. Each item is one batch, one owner, and a census
delta — this is where the 533 findings get spent, highest-leverage first. The order
below is by *derivation gain*, not raw count.

1. **P39 Missing Mixin (54)** — a capability re-implemented in N places instead of
   mixed in. Fix: declare the mixin, `AddClassBaseOperation` on each consumer,
   delete the copies. Evidence: `NoExactCopies` on the affected bodies + census P39
   down.
2. **P37 Restated Identity (74)** — one fact stored twice. Fix: one owner, the rest
   derived (`DeriveClassFamilyCollectionOperation`,
   `DeriveAutoregisterInstanceViewOperation`). Evidence: census P37 down + affected
   consumer suites.
3. **P40 Constant Subtype Answer (47) + P47 Name-Only Subclass (44)** — a leaf that
   returns a constant and a class that only differs by name. Fix: put the answer on
   the member, or collapse the class with `CollapseRedundantClassAuthorityOperation`.
   Evidence: census deltas + family tests.
4. **P49 Own-Class Type Test (46)** — `isinstance(self, X)` / `type(self) is X` in
   place of polymorphism. Fix: a method on the base, an override on the member.
   Evidence: `PatternAbsent` for the exact shape + census P49 down.
5. **P38 Duplicate Class (42) + P1 Nominal Boundary (40)** — merge only where
   Theorem 4.8 holds (every answer agrees in every reachable state); otherwise share
   the implementation and keep the identity. Evidence: the merge decision recorded
   per pair, not per class.
6. **P43 Zero-Or-One Tuple (35), P44 Threaded Absence (33), P33 Foreign Absence
   Probe (24)** — one absent-vs-present decision encoded three ways. Fix: a declared
   family with a `No*` member (`NoAbsenceProbes`, `ClassFlagsAbsent` as guards).

Each item: **no batch without a before/after census number in its PR body.** A
finding that moves without the number is not evidence.

### P6 — Close the loop

- Re-run the full detector scan; the plan is done when every phase's census delta is
  on main and the remaining findings are listed as accepted, with a reason each (an
  item that is genuinely a boundary, not a family).
- Delete the old `PackageStructurePhase` predicates only when the new ones subsume
  them, or replace them so no phase can be satisfied by a file disappearance again.

---

## 5. Working protocol (how a session runs this)

1. **One complete scan is the map.** Don't rescan per edit; rescan when a module
   boundary moves or a new package appears. Note: the analysis cache reports
   `partial` even on a repeat scan (`nra-full.json` → `timing.analysis_cache_status`),
   so a rescan is ~15 s, not free — plan rescans, don't sprinkle them.
2. **Ownership trace before any edit.** For the batch: declaration, every
   implementation, every consumer, every projection. `OPEN` rows stay visible; a
   lead is not proof.
3. **Compose a trajectory, don't hand-edit.** NRA's declared operations do the moves
   (`AddClassBaseOperation`, `ExtractAuthorityOperation`,
   `PromoteExactLeafMethodsToAncestorOperation`,
   `DeriveClassFamilyCollectionOperation`, `CollapseRedundantClassAuthorityOperation`,
   `EnsureImportOperation`, `RemoveImportNamesOperation`, `CreateFileOperation`,
   `PatchTargetOperation` — 69 operations in total, catalogued per checkout), with
   `--codemod-synthesize-plan` / `--codemod-preflight` / `--codemod-simulate` per
   stage and one revision-checked apply. A hand edit is allowed only where an
   operation does not exist, and says so in the PR.
4. **One combined diff per batch**, reviewed against the evidence in §2.
5. **Never block the owner on a long command.** The finding scan is 15 s; the
   economics/plans mode is minutes and is a *background* job, never foreground.
6. **Report the four layers separately** — coverage, ownership, replay, behaviour —
   and say plainly when one is missing.

---

## 6. OPEN questions (need Tristan, not more scanning)

1. **Order:** `projects/` before `messaging/` (P2 before P3)? The plan assumes yes,
   because `repo.py`'s closure is smaller. Say the word if the conversation surface
   hurts more.
2. **`integration.py`** (1,428 lines): project integration, or a third thing with
   its own destination? Its name cannot decide it; the trace can, but a hint saves a
   pass.
3. **`todos.py` split (P1) vs #4214 (`ToolRequest`)**: one PR chain or two? The plan
   assumes two, #4214 first, because it is already open with a measured defect
   behind it (1.5 s import through `comms`).
4. **Slop budget:** is all 533 findings in scope, or do we commit to the top five
   patterns (315) and record the rest as accepted debt with a reason each?

---

## 7. Non-goals

- No new abstraction layer, no service, no registry, no "utils" package.
- No renaming for its own sake: a move keeps declared names and durable tags.
- No behaviour change in a move batch. A move that needs a behaviour change splits
  into two PRs.
- No test rewrites on the way through: the owning family must pass unchanged, which
  is the evidence that the move was a move.
- No satisfying a phase by deleting its evidence predicate (the failure this plan
  exists to correct).
