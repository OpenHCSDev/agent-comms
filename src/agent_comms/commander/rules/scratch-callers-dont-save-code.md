---
name: scratch-callers-dont-save-code
description: "a scratch/check script calling a method is never a reason to keep it; delete the method and fix the scripts"
type: feedback
---

When tracked code is being deleted and some scratch or check script calls it, delete it anyway and order the scripts fixed. Scratch follows the tracked code, never the reverse.

**Why:** 2026-10-19 I pulled my own correct order to delete TerminalApp.text() (P699) after a fork found two toad-theme-2 scratch scripts calling it. Tristan: "just because a script calls shit to be deleted doesn't mean u put it back, u fix the scripts."

**How to apply:** when a fork or unit reports that a deletion has callers, check whether those callers are tracked production code. If they are only scratch or check scripts, keep the deletion and add an order to fix those scripts. Related: [[cleanup-in-same-change]].
