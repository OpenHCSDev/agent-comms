## Summary

Complete original S4 builtin-channel literal/caller closure on main202.

- Use existing BuiltinChannel for TurnRunner defaults, selected claim publication/release, participant alias replies and saved-transcript attribution.
- Delete GLOBAL_TARGET, GLOBAL_CHANNEL and unused BROADCAST_ALIASES; migrate all current core callers.
- Preserve boundary golden spellings, saved data and routing/admission semantics.
- Keep TurnRunner to import/default/constant hunks for Darwin O1 integration.

## Local acceptance

104 distinct affected cases covered: initial99 pass plus2 repaired stale activity-fixture cases and3 alias/routing boundary cases. Actual local subprocess and private bus/SQLite admission seams included; no provider rerun. Original failure log retained. Full-context NRA79complete/0omitted; post-change AST literal census leaves only the declaration-owned spellings.

See evidence/s4-builtin-closure/HANDOFF.md for exact files, commands, removed names and limits. Parent owns integration/deployment; CI deferred.
