# Headless UI integration

Current owners, source progress, installed qualification and live delivery are
listed in [the shared integration checkpoint](../cleanup-20260929/16-CURRENT-S14-OWNERS.md).
This package uses that record directly rather than maintaining a second owner
table that can disagree with it.

The original supplied plan remains in this directory. Its eeb328da source counts
are historical; source decisions use current main and current PRs. Keep U1–U8's
remaining requirements with their existing implementation owners. A qualified
checkpoint does not complete the full extraction or performance scope.

Frontend choice follows the plan's route measurement and acceptance order. Keep
Textual live until another frontend earns acceptance. Useful qualified fixes
ship independently of that choice. No new worktree or environment per surface;
source reasoning first, coherent implementation, affected installed checks last.
