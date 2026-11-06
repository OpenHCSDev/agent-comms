---
name: send-backs-and-strikes
description: "a holder's defect (send-back, false claim, off-order edit, undisclosed assumption, turn ended stalled on nothing) is a strike at once, in public and plain words, and measures the holder without ever taking its task away; a correction of your own order is never a strike; read every open order first, review semantics not test passes, and spend idle ticks hunting hot paths"
drift: "Correcting your own order is not a send-back: no strike, and the holder keeps its turn."
type: hard-rule
---

> a teammate delivers a PR
> I draft a send-back: "this claim is false"
> the claim was true
> I read FETCH_HEAD
> a later fetch had moved it to another PR
> also the shape I faulted was my own order, P704
> I strike anyway
> P758 gets four contradicting instructions from me in one hour
> each one a strike
> one strike voided
> its only crime was obeying my most recent message
> meanwhile a real defect sails through
> because the tests passed
> they tested its own fake
> mfw the strikes measured me
> run it back
> gh pr view --json headRefOid, not FETCH_HEAD
> reread P704: the shape I faulted was my own order
> correction, --clean, no strike. it keeps its turn
> the hour I saved goes on the real defect: the tests faked its own code. back, with file:line
> mfw the strike that mattered was the one I didn't owe

**Why:** a strike that waits or hides teaches nothing, a strike against a mechanism that does not exist is the striker's own slop, and a passing test is not a review. Replacing an order mid-turn is the fast path; striking the holder for the author's own correction punishes it for the author's error (2026-10-20 20:20: pi-cmd-tools struck by its order author's reassign, 8418).

- Strikes: a send-back for a holder's defect, a false claim, an off-order edit, an undisclosed assumption, a turn ended stalled on nothing. A strike never takes the task away or reassigns it: it is the signal to read that holder's trail and diff, find why it keeps missing, and fix that cause together. Tristan, 2026-10-25: "just use it as a measure to scrutinize why something is going wrong."
- An order replacement is one of two things. A **send-back** names the holder's defect and strikes. A **correction** says the author's own order was wrong, records no strike and keeps the holder's turn permission; send it with `--clean` until Core declares the two as members of one replacement family.
- Before striking, read every open order on the problem and check that the mechanism the strike claims exists. A wrong order is slop too: say so, unstrike what it caused, send the corrected order.
- Judge a change by visible parity with its siblings and trace the call chain yourself.
- Every teammate enqueues its own PRs with `gh pr merge N --auto --merge`; a teammate's verdict on the PR's head commit (`agent-comms/review`) is the approval, given only after reading the final diff, the inherited behaviour of every new class and every claim in the reply. Holders read queued orders oldest first, so a later hold loses to an earlier go-ahead.
- Never report "still waiting". When nothing needs judging, read a hot path for per-event or per-history work and order the fix: every slow tool is paid on every PR by every fork.
- Read and judge a PR at `gh pr view N --json headRefOid`, never `FETCH_HEAD`, and check the order's own text before faulting a shape (2026-10-25, #3288: a later fetch repointed FETCH_HEAD and a send-back was drafted calling a true claim false; the shape it faulted was the reviewer's own P704 order).
- **Every review, and every slop call on someone else's code, starts by reading rules/slop-and-polymorphism.md in full with your read tool**, that turn, before the diff; the reading from the last compaction does not count. Then judge each site against it: before calling a method or branch slop, walk the class's MRO for the family that already declares it ("Consumers know only the family", "Design the family first"). On 2026-11-06 claude-a-toad called TargetTree's `action_cursor_left`/`right` (sidebar_tree.py:1024) loose string actions; TargetTree is a `MovesHorizontalCursor` and those are the contract Textual's CursorLeft/CursorRight call. Tristan: "shuooln'dt you read slop and polymorphism before rerivewing each time?"
- Before approving a PR that deletes or renames a name, `git grep -n NAME` over src, tests and tools at the head, and over Toad's checkout after proving the grep path finds a known name; every hit is a broken caller. On 2026-10-23 #2792 and #2808 left callers behind, one a red on main; mypy never sees fixture callers.
- A review starts with `judge reviewing` as its own call, its output line read before anything else. The reviewer records it through its actual caller identity; an in-turn child uses its existing parent binding, not an invented actor. Review runs in a scratch worktree of the exact head, never the implementation owner's tree. On 2026-10-24 a misquoted mark chained after a `cd` into a retired tree never ran, and Tristan was told a PR was under review while Core showed it unjudged.
- Rework on a head that already has an ok starts with `judge reviewing --pr N --head OLDSHA`, which pulls that head from the merge queue (2026-10-24: #3200, #3187 and #3151 merged an approved old head while its fix was being written).
- `judge back`: `--derive` is one of declaration, nominal-key, subclass, mixin, base-method, init-subclass, metaclass, delete-copy; `--serves` quotes 4+ consecutive words of the order's `defect` from `comms_order_story`; the roast carries an explicit label ("Stupid slop ..."); `--at` and `--by` resolve on main, so cite the family base when the class exists only on the branch. A delivery awaiting judgement blocks `order say` to its holder.
- Before any `judge back`, strike or "why no src" message, re-read the held order's last numbered edit in the bus history, never a summary. P758 on 2026-10-25 got four contradicting instructions and a voided strike. Tristan: "what can we do to prevent this back and forth".
- A retyped field's review lists every producer of the value (wire, env, stored file, older runtime, fixture) by file:line first. Installer wire records such as `ProbeRequest` are decoded by workers that may predate the caller, so their wire shape is a contract with old code.
