# Unfinished retained-bus update — replacement owner needed

Tristan stopped this implementation and requested an issue plus linked draft PR for another agent. **Do not run the one-off operation scripts as an approved installer. Do not merge or install this branch without review.**

Draft: https://github.com/OpenHCSDev/agent-comms/pull/1355
Branch: `chore/refresh-package-mains`
Checkout: `/home/ts/wt/comms-update-latest`
Original source checkout: `/home/ts/wt/comms-update-original` at `04e06f6fd`, with the original installation's local native manifest copied into it.

## Requested result, still undelivered

Update the main-owned editable Core installation and Toad/Textual/ui-theme from current mains, using the existing retained bus. Carry its schema safely, preserve sessions/history/auth/UNKNOWN inputs, and verify actual installed Toad. No development worktree may become the live installation.

Live main was last checked at `04e06f6fd`, with only the pre-existing `stack/pi-native.sha256` modification. Active route selects `/home/ts/.agent-comms`, private root ID `f310c1649c9448288a05344344dc76b9`. Latest Core cannot decode its registry: `TypeError Missing required fields for Thread: storage_roots`. All 13 records still use the original storage schema. Do not pull incompatible editable Core over live readers first, and do not substitute an empty bus.

## What I actually tried

- Prepared a package-main lock refresh and published this draft.
- Built candidate native resources. Found source resource selection incorrectly preferred a stray checked-in `_native/core_stamp.py` over checkout stack resources. Proposed source-selection fix, deleted that unused absolute-path stamp, added regression coverage. All 28 native-package tests passed at that checkpoint.
- Native preparation initially failed resource lookup, then the full-tree commitment. Re-prepared with `--update-pins`; candidate verification succeeded. This changed the manifest's full-tree commitment, not the stock Pi package. Candidate artifact is under its ignored `stack/.pi-native-*` directory; derive its exact path with `pinned_native_package()`.
- Split each existing carry into `carry_locked` plus its original wire-owning CLI, to avoid reacquiring the stopped batch's retained wire lock.
- Rehearsed all three carries in order on disposable copies: storage (13 records), goal publications, then goal waits. A second rehearsal created an original-format goal only on the disposable copy; both goal records carried and new Core read the migrated goal and input dispositions. Copies were removed. Original root was not migrated.
- Integrated fetched main normally into the branch.

## What I messed up

I expanded routine installation work into three bespoke scripts under `upgrade-evidence/operation/` plus large AST evidence instead of delivering the update through the existing owners. They are an **unexecuted, unaccepted proposal**, not a working maintenance path. Late edits added phase changes, recovery checks, native-source preimages, storage registration and a post-restart goal; these additions have not received end-to-end verification. The scripts would stop owners/Toad, change main and global managed extension sources, migrate data and launch processes. Review or delete them; do not casually execute them.

The final stack metadata edit also removes obsolete `[acp]` and equal-version constraints so package mains own their versions. The subsequent lock/sync tool call returned no retained result before interruption. **Do not claim the final pyproject, lock and environment are synchronized.** The earlier lock resolved; that is a narrower result.

## What has NOT happened

No live cutover controller was launched. No maintenance hold or continuation goal was created by these scripts. No owners were stopped by this work, no live schema carry applied, and no route switched. No original/UNKNOWN input was replayed. The latest installation and actual Toad acceptance remain undone.

The draft includes the full unfinished implementation, including the late edits, for review. CI and full-branch acceptance were not run. Native-package tests and copied-root rehearsals do not establish installed correctness.

## Replacement-owner acceptance

1. Inspect active owners/frontends and retained uncertain inputs; use existing stopped-owner/maintenance ownership rather than stacking more orchestration.
2. Validate required schema/native boundaries and package-main resolution. Preserve quiet preimages and actual session custody.
3. Perform the necessary migration and installation into main, not either worktree; restore only previously running owners without replaying uncertain input.
4. Exercise installed saved-history startup, attachment and the actual Toad journey; distinguish merged, installed and verified.
5. Retire these owned disposable artifacts after borrower checks and acceptance. Do not touch Codex rollout/session originals.

Owned build scratch: `/home/ts/.cache/agent-scratch/codex-agent-comms--upgrade-native` (OWNER.md records purpose). Both worktrees remain for review; neither is the live installation. Any later issue/PR update supersedes older ownership.md statements that implied imminent execution.
