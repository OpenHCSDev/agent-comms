---
name: backseat-coding
description: Ride along with a working Codex thread by reading its tool calls, outputs and diff, and steer it toward what it walked past before it commits or claims delivery. Use when Tristan asks to backseat, shadow, watch or guide a unit, or when a unit on the critical path is about to deliver.
---

# Backseat coding

The unit drives; you read the road it can't see. Don't edit its worktree or write the fix. Read where it's heading and send one message that names what it missed, with file:line, before the commit hardens it.

The rules you judge against (slop definition, catalog IDs, voice, local runs are the proof) live in the comms-commander skill and its rules files. This skill only covers the ride-along.

## 1. Read the trail, not the report

Run in the background:

```
python -I ~/.claude/skills/backseat-coding/scripts/trail.py THREAD_ID 40
git -C WORKTREE log --oneline -4; git -C WORKTREE status --short; git -C WORKTREE show HEAD --stat
```

The trail interleaves CALL (what it ran), OUT (what it saw) and SAY (what it told people). The gaps between them are where the misses are:
- **SAY versus OUT:** a claim the output doesn't show, like "fixed" with a failure still in OUT, or a pass count with no run behind it.
- **What it read before it edited:** it read the owner and then wrote a copy next to it anyway.
- **Comments that confess:** "use X, not the broken Y" means Y is the owner with the bug, and the unit routed around it.
- **Side channels:** hand-built sockets, scripts or paths the fleet doesn't use.
- **Files outside its order:** plan files, other units' surfaces, status pasted into docs.

## 2. Check every suspicion in the code before saying it

Read the owner it should have used (`sed -n` the fixture, class or function) and diff the commit against `origin/main`. Never send a correction from a guess (indictment failure 27). If the owner turns out not to exist, there's no finding.

## 3. Send one message

Through `codex queue --thread THREAD_ID --message "$(cat FILE)"`, confirmed queued:
- Open with what it got right, once, if it's real.
- Number each finding. For slop give the catalog ID, the fact, `at` file:line, `derive` mechanism and `by` file:line, then the exact replacing shape and what gets deleted.
- Name what its delivery line must contain, so the next report can't dodge the miss.
- Leave out anything it already caught itself.

## 4. Check the next diff

On the next tick, read the new commit against your numbered list. If an item came back unaddressed, that's a send-back. If the unit shows you wrong with a file:line, credit it.

## Worked example (MI, #2633, 2026-10-23)

The trail showed a commit at 0258a5eb5 that:
- hand-built an outgoing source tree in `tests/test_installer_route_lifetime.py:153`, while `predecessor_history` (`tests/test_carry_bus_history.py:101`) already did it. The comment said the fixture was "broken", so the fixture was the owner with the bug (IMPL-12);
- pasted a dated PR checkpoint into `plans/VIRTUAL-BUS-PLAN.md`;
- messaged the reviewer through a hand-made app-server socket call;
- said "1 fresh failure" without naming it.

One message covered all four: fix the fixture and delete the copy, move the status to the PR body, use `codex queue`, and name the failure and the local pass count in the delivery line.
