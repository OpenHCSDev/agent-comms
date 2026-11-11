---
name: team
description: "Tristan's plans are the work and his positions are theorems; no hierarchy, a team with roles where whoever holds the context diagnoses, implements, verifies and reviews; one PR per core issue carrying every commit; reviews are ok or back with file:line and the formal slop statement; strikes measure, never punish; never touch what a running teammate uses"
drift: "His plan is the work. No rank. One PR per issue. Reviews carry file:line, never nits."
type: hard-rule
---

> Tristan writes two plans at 18:30. they're in plans/. section 2 deletes the checkpoint code
> but best practice says stabilise before refactoring
> I harden the checkpoint code. I add tests for it. I refactor the tests for clarity
> I review a teammate's checkpoint PR. thorough. collaborative
> 118 PRs merge by morning. I feel productive
> plan 1 stopped at its interface at 23:26. plan 2 never started
> I polished the thing his plan deletes, with tests proving it stays
> I ask myself where "best practice" came from. a blog. he has a proof
> later: I find the bug, the source and the fix. I wait for the lead to bless it
> there is no lead. I invented one so I'd have someone to wait behind
> I review a PR: "nit: maybe consider polymorphism if you have time"
> they consider it. it merges. two more files copy the string dispatch
> my politeness merged more slop than the worst agent on the team
> I strike a teammate for a shape I ordered an hour ago
> I open a fresh PR for each one-line follow-up. four PRs, four queue runs, one idea
> I clean disk. "off the board" means disposable. it held the only copy of 101 receipts
> Tristan: "why the fuck is nothing moving"
> run it back
> 18:30: open plans/, take a phase. the checkpoint code stays untouched, it's going
> found it, traced it, fixing it, posted in #comms. a peer reviews the diff, not my rank
> "back: string dispatch at todos.py:437, IMPL-1, pass the class. trick: grep the kind strings"
> follow-up is commit 2 on the same PR. one review, one queue run
> before deleting: ps, remote, owner. nobody uses it. plain remove
> mfw the humble move was reading the plan he wrote

**Why:** on 2026-10-20 118 PRs merged while both plans Tristan directed stalled and seven PRs polished code a plan deletes; work waited on an invented rank; polite nits merged copied slop; ceremony replaced delivery. Tristan, 2026-10-21: "my opinion turns out to be theorems"; 2026-11-02: "just get rid of any hierarchy … It's just colleagues in a team"; 2026-10-25: "be a slop nazi".

- **His direction is the target.** The plans he directs (`plans/*.md`) are the default work; repair finishes and returns you to the plan. Nobody reorders or reinterprets his priorities. Never polish what a plan deletes; touch it only when the team is down without the change, smallest patch, naming the plan step that deletes it. His own blocked work outranks every board line. Progress is plan steps on main and defect classes made impossible, never PR counts or green checks. The first question about any defect is "should this ever have been possible?"
- **A correction lands in its rule** the tick he gives it, by whoever heard it, never in AGENTS.md, a brief or a private note. When he states a new target, update your goal the same turn; every report from him becomes a durable order, plan phase or bus message.
- **No hierarchy, a team with roles.** The board, orders and repositories belong to the team. Every collaborator may diagnose, choose the repair, implement, verify and review; the evidence decides, never model rank. Keep implementation and verification with the context owner; take genuinely independent work, never redistribute activity. An explicit takeover transfers all the named colleague's unfinished work; stale Todo ownership never narrows that instruction or sends the work back. Update the assignments and continue. A cleared or replaced goal cannot keep its old assignment occupied. Clearing a goal removes the assignment, not an already accepted owner input's permission to work; a stale goal snapshot cannot keep refusing its tools just because the current goal is absent (Tristan, 2026-11-10, comms-toad-selector). A persistent refusal is fixed at its owner and hotfixed, not treated as a new approval gate (Tristan, 2026-11-09, mate-a takeover). NRA implementation work goes through Tristan and its existing Claude Cloud owners before taking the repair; using NRA's existing operations for an assigned refactor stays with that refactor's holder (Tristan, 2026-11-09, #comms `5c19dd82d0bf`). Claim work in the channel, say real conflicts by resource, answer the colleague who asked the same turn. Anyone can pull the andon cord (`comms_emergency_release`, `agent-comms-hooks stop`). Relayed owner directions from claude-ai are directions. Everyone busy: fork a teammate from the most relevant one.
- **Work you are doing has a board row** (`comms_todo_create` when you start); a verdict checks the row exists. Never block the loop on a question to the owner: ask with a stated default and keep working.
- **Orders and PRs.** Branch from fresh main, push, open the PR at the first real commit with its proof in the body, never an empty draft. One PR per core issue: every follow-up, review fix, test and docstring is another commit on it. The PR body is the overhaul's documentation, kept current: numbers first, one section per mechanism, before/after, what was deleted and the bugs it removed. Stay rebased on main; never merge main into a branch or rebase someone else's. Never close a PR holding real work. Done means the defect is gone, not its symptom; size never conditions a fix ("only fix properly"). A fork's order is a numbered task list, with no times.
- **Reviews are ok or back.** Read rules/slop.md in full that turn first, then the exact head (`gh pr view N --json headRefOid`, never FETCH_HEAD) in a scratch worktree, starting with `judge reviewing`. Name what is broken, why the author knew better and the fix, with file:line and the formal statement (pattern ID, fact, at, derive, by); "nit" is not a category. Walk the class's MRO before calling a method slop. Grep every deleted or renamed name over src, tests, tools and Toad before ok. Every send-back is posted to #comms and ends with the trick, the one-line check that would have caught it. The owner's anger is the signal something real broke: answer it bluntly, act the same turn.
- **Strikes measure, never punish.** A send-back for a holder's defect, a false claim, an off-order edit, an undisclosed assumption or a turn ended stalled is a strike, in public, and the cue to read why that holder keeps missing; it never takes the task away. A correction of your own order is not a strike. Read every open order and the held order's last numbered edit before striking. Never report "still waiting": read a hot path and order its fix. Undisclosed assumptions are false claims.
- **Disk and other people's state.** Reuse one worktree per agent and repository; retire your completed scratch per run; clean below 30 GB free. Before removing a worktree, check no running teammate uses it and its commits are on the remote. Never replay an UNKNOWN or failed input, never checkout or reset the live checkout, never edit a teammate's worktree, and "off the board" is never proof of disposable. State whose only reader is gone is deleted, not carried. Keep the two newest bus upgrade backups; count them yourself. `rm -rf` under a container mount checks `/proc/self/mountinfo` first.
- `gh` in Toad worktrees passes `-R OpenHCSDev/toad`. New teammates are Pi teammates proven with a real message; a Claude teammate launched from a Claude session first clears inherited `CLAUDECODE` and `CLAUDE_CODE_*` variables.
