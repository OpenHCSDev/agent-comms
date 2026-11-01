---
name: fork-orders
description: "every fork or unit order carries the invariant steps up front: PR at the first real commit, never an empty draft, land through the merge queue, no local NRA gate runs or full suites, delete slop instead of rewriting it, one version of each system"
drift: "No silent errors, no deferrals, no follow-up: done means the defect the problem names is gone."
type: feedback
---

> fork starts work
> no branch pushed
> no PR
> works two hours
> it is excellent work
> session ends
> it is gone
> next fork opens a draft PR first, like the rule says
> empty
> does nothing else for an hour
> "PR is open"
> it is a PR in the sense that a parking space is a car
> next fork lands a fix
> then a follow-up PR for the one-line fix it found in review
> then a PR for the test
> then a PR for the docstring
> four PRs, four reviews, four merge-queue runs, one idea
> merge queue: 40 minutes deep
> the follow-up fixes a bug the first PR introduced
> it is waiting behind the first PR
> mfw I turned one change into a supply chain
> run it back
> branch, push, PR at the first real commit
> review finds a one-line fix: commit 2, same PR
> the test: commit 3. the docstring: commit 4
> one review, one queue run, 40 minutes not spent waiting behind myself
> mfw the supply chain was `git commit` the whole time

**Why:** the commander writes orders from habit, and habit ends with "push, PR, merge"; on 2026-10-20 five forks held uncommitted work with no PR, a closed PR threw away two hours of good work, and orders said to run ratchets locally after the merge queue already ran them. Tristan: "branches should always have prs for them, if you don't it gets lost"; 2026-10-24: "always make a pr before starting to no allow brnches to disappear"; "don't have two versions of a system when one supersedes the other"; "no backwards compat".

Every order starts with these steps, before its own:
1. Before any work: branch from fresh main and push the branch, verify `git ls-remote` equals HEAD. Open the PR at your first real commit, ready, with its proof in the body; never an empty or placeholder draft PR. Every commit after that is pushed to it. Tristan, 2026-10-26: "you shouldn't tell them to open a draft empty pr then do nothing thats called being stupid"; "no open draft prs ... thats the goal"; "so we know that the pr represents work that needs to be done".
1a. **One PR per core issue, carrying every commit for it.** A PR is the documentation of one overhaul: every change that serves the same core issue lands as another commit on that PR, never as its own PR. A one-line follow-up, a fix found in review or the next step of the same refactor is a commit, not a new PR. Tristan, 2026-10-27: "every tiny little stupid poo-poo pee-pee penis change has gone through all of the ceremony instead of just keeping all of the changes related to the one core issue in one PR"; "some of these PRs give a whole fucking facelift to OpenHCS and serve as documentation".
1b. **The PR body is that overhaul's documentation, kept current as commits land.** It opens with a summary in numbers (commits, lines deleted, what went from N to M), then one section per mechanism changed, a Before/After of the class structure, what was deleted with line counts, the bugs the change removed and why they existed, and the measurements. A reader who never saw the code should understand the system from it. Model: OpenHCSDev/openhcs#58 (+22,245/-54,828 across 383 files; "PFM 1209 -> 621 lines", "LiveContextService 364 lines -> 0"). Tristan, 2026-10-27: "some of these PRs give a whole fucking facelift to OpenHCS and serve as documentation".
2. Land through the merge queue: mark ready, `gh pr merge N --auto --merge` (gh refuses a bare `--auto` when it runs non-interactively). Never run the NRA gate or the full suite locally.
3. Delete slop instead of rewriting it: tests that patch our own code, wording asserts, tests of deleted code, the superseded copy of any system. No compatibility shims or aliases.
4. Stay on current main: fetch before every commit and merge origin/main the moment it moves; resolve every conflict yourself. Tristan, 2026-10-20: "remind units to continuously sync to main so they don't work on outdated checkouts".
5. Never close a PR holding real work; land its good parts.
6. Report: PR, merge commit, what was deleted, and an `Assumed:` list.
7. Done means the defect the problem names is gone, not its symptoms. Every order spells out its done list (A, B, C...); a delivery missing any item is a send-back. A narrow re-read that merges while the live failure stands, "deleted in a follow-up", and a handoff with failing verification are open ends with an owner today, never a delivery. Tristan, 2026-10-20: "dont allow half finished work. be strict and be explicit as fuck".

A fork has the commander's context, so its order is a numbered task list to start now, with no time. Tristan, 2026-10-20: "stop giving it times give it tasks to do now".
- Size never conditions a fix. No brief, order or message says "if quick", "quick fix" or "small fix": every diagnosed defect ends that tick as a draft PR or a unit order, and size only picks who types it. On 2026-10-25 a brief saying "fix it if quick, else report the fix shape" ended two forks as prose with no PR. Tristan: "nah no more fix if quick, only fix properly, who start saying we only fix if quick???????????"
