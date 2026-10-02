## Ready: goal and coordination attempt ownership

**657 production lines deleted /649 added.** GoalAttemptStore781→490lines; AttemptStore512→298. Existing declarations own their actual behavior:

- Reservation/LaunchPermit: failure, claimed-state proof, verified progress/completion and usage attribution.
- Generation/AttemptRecord/GoalHumanDecision: typed read, checked transition, digest interpretation and single decision consumption.
- AttemptStart/AttemptState: activation, phase/finality and renewal.
- Terminal phase cases + RecoverySnapshot: typed outcome choice and atomic coupled settlement.
- ReplayAssessments: monotonic evidence recording; UNKNOWN cannot become retry authority.

Stores retain connection/transaction and process-local capability custody. Removed methods and every caller migrated, no facade aliases, new journal/schema, converter, prior-format reader or replay. Current main13ff0385 includes381/382/383. Boyle384 reviewed READY separately; no concurrent SelectedExecution implementation.

### Evidence

-153 focused crash/SQLite/goal/recovery checks PASS67.56s.
-2 actual installed native journeys PASS22.20s: retained goal history through failure, passive read, socket explicit Retry and one successful new continuation; retained selected EOF/UNKNOWN then only a fresh input.
-11 final installed checks PASS14.94s after383 integration, including the actual saved-native goal journey, ownership guards and invalid/live-phase terminal refusal.
-Noneditable wheel; canonical9213 manifest verified; zero external provider calls.
-Ratchet: no increases, god-class excess−293, foreign absence−11, type identity−1. Patterns IDEN-1/3, IMPL-4/5/10/12, BOUND-1, MEMB-5, TIME-1/3.

[Complete receipt including retained failures](evidence/review384-attempt-ownership/README.md). Actual installed testing caught one missed ordinary-goal caller before raw input; fixed on LaunchPermit and guarded. Later broad-selector failure was the old Done/Reserved fixture, corrected by integrating383; not called a wholly green broad run.

Source ready for parent review/merge. Parent owns paired live acceptance and deployment; no live edits, reset, replay or CI wait. Completed scratch cleaned after process checks; source/evidence/current candidate and canonical packages retained.
