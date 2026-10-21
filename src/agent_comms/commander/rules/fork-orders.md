---
name: fork-orders
description: "every fork or unit order carries the invariant steps up front: draft PR at the first commit, land through the merge queue, no local ratchets or full suites, delete slop instead of rewriting it, one version of each system"
type: feedback
---

**Why:** the commander writes orders from habit, and habit ends with "push, PR, merge"; on 2026-10-20 five forks held uncommitted work with no PR, a closed PR threw away two hours of good work, and orders said to run ratchets locally after the merge queue already ran them. Tristan: "branches should always have prs for them, if you don't it gets lost"; "don't have two versions of a system when one supersedes the other"; "no backwards compat".

Every order starts with these steps, before its own:
1. At the first commit: push, verify `git ls-remote` equals HEAD, open a draft PR. Keep pushing to it.
2. Land through the merge queue: mark ready, `gh pr merge N --auto` (the queue refuses `--merge`). The queue's hosted ratchets are the only ratchet run; never run them locally, never run the full suite.
3. Delete slop instead of rewriting it: tests that patch our own code, wording asserts, tests of deleted code, the superseded copy of any system. No compatibility shims or aliases.
4. Stay on current main: fetch before every commit and merge origin/main the moment it moves, so nobody builds on an outdated checkout; resolve every conflict yourself, never send it back. Several conflicting PRs go to the merger as one batch. Tristan, 2026-10-20: "remind units to continuously sync to main so they don't work on outdated checkouts, this also saves a lot of problems".

A fork has the commander's context, so its order is a numbered task list to start now, with no deadline: a clock adds nothing to work that already knows its steps. Tristan, 2026-10-20: "stop giving it times give it tasks to do now".
5. Never close a PR holding real work; land its good parts.
6. Report: PR, merge commit, what was deleted, and an `Assumed:` list.
7. Done means the defect the problem names is gone, not its symptoms cleaned up. Every order spells out its done list (A, B, C...), and a delivery missing any item is a send-back. Withdrawing stale rows does not deliver "Core hands rows out one per wake"; a narrow re-read that merges while the live failure stands does not deliver the failure; "the tool is deleted in a follow-up" and a handoff with failing verification are open ends with an owner today, never a delivery. Tristan, 2026-10-20: "dont allow half finished work. be strict and be explicit as fuck".
