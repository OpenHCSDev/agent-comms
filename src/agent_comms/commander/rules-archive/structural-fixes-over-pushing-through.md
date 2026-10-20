---
name: structural-fixes-over-pushing-through
description: "When a demo or feature exposes a platform gap, fix the platform at its owner instead of working around it"
type: feedback
---

When work on a feature or demo exposes a gap in the platform, fix the gap structurally rather than working around it to get the feature through.

**Why:** owner, 2026-10-12, on the Cellpose/N2V2 custom-function investigation (draft PR #1230), which listed ten platform gaps: "do all the fixes. structural fixes are more important than pushing through and ignoring them." The same session: an ObjectState defect found in passing ("shouldn't it be fixed regardless?") and the single-owner global config clean-up ("we should do that larger cleanup, we're already deep in this").

**How to apply:** treat workarounds found during a feature as a work list, not as acceptable cost; dispatch the fixes without asking, each at its owner, and make the feature's examples the acceptance test (no workaround left). Report a gap as "fixing" rather than offering it as an option. Relates to [[optimize-for-slop-elimination]] and [[full-autonomy-openhcs]]. 

**No narrow fixes (2026-10-19):** I scoped the Pi tracked-turn bug as a narrow fix and parked the owner's runtime refactor (#1876) behind desktop row agreement. Owner: "why would you not do the refactor along the fixes you did???" / "fuck narrow fixes have you not been listening to me?" A bug inside code a planned refactor covers is fixed *as* that refactor's step, in its target shape; anything not blocked by another owner starts now. Splitting by owner is fine; using the split as a reason to do only the minimal patch is not.
