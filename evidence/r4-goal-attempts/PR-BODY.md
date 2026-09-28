## R4 original-plan closure

- Extend the existing GenerationState and add the distinct declaration-owned attempt phase family. Decode SQLite lifecycle rows once into typed Generation/AttemptRecord values with FieldCodec.
- Route generation/attempt mutations through declaration-checked transitions. State owners determine retirement eligibility; derive DDL choices and lifecycle SQL predicates from declarations.
- Delete Generation's string constructor and `.state`, raw tuple/status dispatch, and the StorageUncertain/ReservationConflict/UnresolvedAttempt/StaleAttempt aliases. Migrate core callers and tests in the same change.
- Preserve schema5 saved data, existing data migrations, grants issued only after commit/fsync/readback, one-use launch permits, transaction fences and UNKNOWN/no-replay semantics.

## Verification

- 78 store/failure cases pass, one Windows-only skip; includes competing processes, crash and fsync uncertainty.
- 171 consumer cases pass; two stale `.state` assertions repaired. Seven focused boundary/repair cases then pass, including those two repairs.
- Five prepared-native localhost retry/cancel/standby cases pass; no provider calls.
- Full-context NRA before/after:79 detectors, complete, zero findings. Concrete caller deletion and behavioral evidence establish closure; scanner count alone does not.
- Main189 (including restart188) merged without conflicts. Reconciled restart/native/runtime process batch:31 pass, including17 real process restart regressions, nine runtime retry cases and five native localhost cases.

Parent owns paired Toad and serial installation. No live roots, runtime, model or provider settings changed. R1–R3 untouched. Read `evidence/r4-goal-attempts/HANDOFF.md` for exact evidence and scope.
