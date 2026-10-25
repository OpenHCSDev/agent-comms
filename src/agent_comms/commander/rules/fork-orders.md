---
name: fork-orders
description: "every fork or unit order carries the invariant steps up front: draft PR before any work, land through the merge queue, no local ratchets or full suites, delete slop instead of rewriting it, one version of each system"
drift: "No silent errors, no deferrals, no follow-up: done means the defect the problem names is gone."
audience: fleet
type: feedback
---

**Why:** the commander writes orders from habit, and habit ends with "push, PR, merge"; on 2026-10-20 five forks held uncommitted work with no PR, a closed PR threw away two hours of good work, and orders said to run ratchets locally after the merge queue already ran them. Tristan: "branches should always have prs for them, if you don't it gets lost"; 2026-10-24: "always make a pr before starting to no allow brnches to disappear"; "don't have two versions of a system when one supersedes the other"; "no backwards compat".

Every order starts with these steps, before its own:
1. Before any work: branch from fresh main, make an empty commit, push, verify `git ls-remote` equals HEAD, open a draft PR. Every commit after that is pushed to it.
2. Land through the merge queue: mark ready, `gh pr merge N --auto --merge` (gh refuses a bare `--auto` when it runs non-interactively). Never run the ratchets or the full suite locally.
3. Delete slop instead of rewriting it: tests that patch our own code, wording asserts, tests of deleted code, the superseded copy of any system. No compatibility shims or aliases.
4. Stay on current main: fetch before every commit and merge origin/main the moment it moves; resolve every conflict yourself. Tristan, 2026-10-20: "remind units to continuously sync to main so they don't work on outdated checkouts".
5. Never close a PR holding real work; land its good parts.
6. Report: PR, merge commit, what was deleted, and an `Assumed:` list.
7. Done means the defect the problem names is gone, not its symptoms. Every order spells out its done list (A, B, C...); a delivery missing any item is a send-back. A narrow re-read that merges while the live failure stands, "deleted in a follow-up", and a handoff with failing verification are open ends with an owner today, never a delivery. Tristan, 2026-10-20: "dont allow half finished work. be strict and be explicit as fuck".

A fork has the commander's context, so its order is a numbered task list to start now, with no time. Tristan, 2026-10-20: "stop giving it times give it tasks to do now".
