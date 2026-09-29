# Original input admission and native compaction

Owner: parent Codex thread. Status: merged, matched installation activated;
native/ACP/UI acceptance completed. Fresh all-owner runtime/participation audit
remains with the parent.

## Reproducer

The live `openhcs-helper` autonomous goal continuation reached native Pi without
an original input reservation. `OwnedTurn.prepare_native` therefore skipped the
journaled compaction mechanism. Stored native context rejected the prompt before
its user-message start. The failed goal attempt remains preserved and blocked.

## Ownership and scope

`OriginalTurnInput` owns eligibility for original-input compaction, inherited by
owner, routed and dependency inputs. `InputDispositions` remains the only durable
input reservation/binding authority. A scheduled input without bus/ACP ingress
gets a reservation derived from its admitted turn identity. Goal launch authority
remains in the existing goal attempt store; a reservation never authorizes a goal.

This removes the owner-only compaction override (IMPL-4, BOUND-2), and stops using
absence of a durable key as a proxy for autonomous input (IDEN-1). No second goal
compaction mechanism, journal, reader, codec or retry path is introduced.

## Acceptance

Use the existing saved-native/ACP goal journey and localhost provider: restore
context that requires compaction, schedule one autonomous continuation, observe
journaled compaction before one native user start, then verify summary and input
proofs, successful goal settlement, and preservation of unrelated uncertain input.
Run this journey against an installed wheel. Check the current baseline fails the
same journey. No user-thread input replay or goal resumption is part of this fix.

## Native and structural results

Installed wheel, real pinned native776, localhost provider: the new cold-saved
goal journey passes. Exactly one journaled compaction precedes exactly one new
native user start; summary progress/publication crosses ACP; goal generation
advances after native completion. Saved bytes and an unrelated reserved input
remain intact. The baseline fails the identical journey with a blocked generation.

The existing failed-goal/retry journey also passes: provider failure is published
once through the typed receipt, the failed attempt remains blocked, explicit Retry
dispatches one fresh continuation, and the older uncertain input is preserved.
Ordinary owner fences/flock and input-disposition checks pass. Seven ownership
guards pass, including a new declaration-derived guard that every original case
inherits reservation and compaction. The packaged ratchet reports no growth;
one long chain and seven boolean terms were removed. Eighteen production lines
were deleted relative to current main (including the owner-only override).

The broader run exposed eight failures in the older adaptive test file: obsolete
nullable row fields/status assumptions and synthetic native history rejected by
current preparation. Mendel owns replacing that fixture with the existing real
native fixture; those failures are recorded, not a claim of a green full suite.

Core411/412/413 and Toad204/205/201 are installed: post-cancel native custody and
typed failure receipts, journaled original goal admission, explicit private launch
authority, same-open-view inbound reprojection, cancellation/Not sent feedback,
and reusable video/profile custody checks. The activated pair is Core
`c1d2849b90023acf5206c35b7da4664407d6dce5`, Toad
`8616aee5f91f8e200c982e02f0ee56d0f49d6c48`, Textual
`412b5a2b5da8875dc2f3dc5be2365abddce0537b` and unchanged native776.

Toad206 fixture/receipt merged as `7e1133a2`. Arendt's exact installed runtime
journey exited 0: real cold saved history, physical `/goal`, painted compaction,
committed summary and answer, one journaled compaction before exactly one native
goal input. Saved bytes remained an unchanged prefix. Physical Pause after native
input start bounded continuation. Seventeen local provider calls included two
history responses, fourteen summary calls and one goal response; no paid calls.
The completed command and receipt are retained in Toad
`docs/validation/cold-goal-compaction-ui-20260929.md` and
`evidence/cold-goal-compaction-staged/`. Core413's production source93f is included
in the exact activated Core pin; the existing acceptance is reused here.

The parent activated `runtime-journaled-original-20260929` and verified the actual
default `toad-comms` PTY: saved history and Ready, exit 0. Ten running owners were
restarted under idle fences in the new Python, with sessions/models/thinking/tags/
goals identical. The parent is verifying fresh all-owner runtime/participation;
this receipt does not predeclare that audit complete. Warm Toad202 remains deferred
under current user priority. Mendel's Core414 owns the eight obsolete fixture
failures above; the suite is not declared green.

Owned disposable scratch: `/home/ts/.cache/agent-scratch/comms-journaled-goal-input-20260929`;
staged install: `/home/ts/.local/share/agent-comms/runtime-journaled-original-20260929`.
The active bus, original failed goal/input and native sessions were preserved.
No original failed/uncertain input was replayed and no blocked goal was resumed.
Only the parent performed the explicitly authorized idle owner restart/activation;
this metadata checkpoint changes docs and paired pins without reinstalling.

## Paired pin verification

`stack/pyproject.toml` and `stack/uv.lock` pin the exact activated Core c1d2849b,
retaining the accepted Toad8616/Textual412 pins. In the metadata owner's isolated
worktree, `uv lock --project stack --check` and `uv sync --project stack --locked
--dry-run --python /home/ts/.local/share/agent-comms/runtime-journaled-original-20260929/bin/python`
both exit 0. `UV_PROJECT_ENVIRONMENT` points to that worktree's nonexistent
`.venv-activation-lock-check`; the dry run creates no environment and installs
nothing. Only the Core revision changes in the lockfile. No full test run,
installation, owner restart or shared-checkout edit is performed by this receipt.
