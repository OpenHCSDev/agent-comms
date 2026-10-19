# Ordinary-turn claims and Toad cancellation

Owner: pi-toad-cancel. Branch: fix/toad-cancel. Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/1919. Do not merge.

## Source decision

The nine blocked source claims on pi-cn-p3 were ordinary acquisitions (bus sequences 1561–1566 and 1568–1570), all with `claim_scope=None`. `OwnedSend.claim_resource` published them; only `RecoverySnapshot.release_resources` retired selected-attempt claims. This was a missing ordinary resource lifetime, not a selected UNKNOWN settlement failure.

`OwnedTurn.stream` now closes its real backend stream before retiring the acquisitions held by its `OwnedSend`, including cancellation and failure. Ordinary retirement borrows NativeBoundary directly in the resource worker; it does not open an unused SQL connection. `ClaimWrites.release_resources` owns the shared bounded exact-acquisition release; selected recovery consumes it too. No schema, timer, owner-name comparison or retry is added. Abrupt process death before ordinary cleanup is not proved by this change; those unbound acquisitions lack a durable ordinary-turn identity.

AST source census used the existing debt-ratchet `ChangedSource.tree` over all 662 Core and installed Toad Python modules. Zero parse omissions. Declaration/call/write sites retained in `/home/ts/.cache/agent-scratch/pi-toad-cancel/ast-before.txt`. Dynamic dispatch is source evidence, not a proven runtime caller. NRA could not import against installed metaclass-registry, and its own Python 3.12 cannot parse this project's 3.14 syntax; no NRA completion claimed.

## Incident evidence

Toad log `~/.local/state/toad/logs/Agent_Comms_2026-10-19T13_08_20_015597.txt` lines 3–10 shows session/load, its successful response, then an outgoing session/cancel before any client prompt. Installed Toad's only production RPC cancel sender is `AgentController._cancel_prompt` (`toad/acp/agent_controller.py:323`). Its production callers are the two-key Cancel action (`widgets/conversation.py:1839–1845`, `conversation_actions.py:43–55`) and the busy `/toad:session-close` command (`slash_command.py:289–298`). Closing that view explicitly calls cancel; attachment/load does not. The log does not record physical keys or action dispatch, so which UI action triggered the incident remains unproved.

## Live repair

Released all nine original stale acquisitions using the branch's `ClaimWrites.release_resources` with their exact original ClaimOwner witnesses under a real NativeBoundary. No database edits. Verified none of those nine generations remains. Left the newer selected tests/test_importing.py claim intact.

pi-cn-p3 confirmed at bus sequence 1606: `functions.edit` successfully changed cli_bridges/history.py (two replacements), no shell bypass, and its current test-file work remained intact.

## Scratch ownership

`/home/ts/.cache/agent-scratch/pi-toad-cancel`: AST evidence and bounded real-process acceptance output, owned by pi-toad-cancel. Preserve any uncertain input/session disposition. Retire disposable runtime copies after the run and keep concise results.

## Real Pi / ACP proof

`tests/ordinary_claims_live_journey.py` uses the configured `openai-codex/gpt-6.1-sol`, high thinking, the real Core owner/native child, scratch bus and two actual stdio ACP subprocesses. The second client uses Toad's initialize capabilities and ExistingSessionLoadPlan. No fake Core/native objects, provider replacement, replay, or event-order assertions. Files are actually written before bash starts. The failure case kills only the acquired scratch native child through its exact process identity; its original UNKNOWN state and file effects are retained.

Executed sequentially under `timeout 300 <shared-stack-python> -X faulthandler`, with PYTHONPATH first `/home/ts/code/projects/agent-comms/src` (live main 8b27e02aa), then this branch's src. Arguments: `--native-launcher /home/ts/code/projects/agent-comms/stack/.venv/bin/pi-comms-native --evidence <owned-stage>`; main also used `--expect-stale`. Source imports and the real exception paths appear in the retained logs.

Pasted observed result (full records in main-owned/result.json and branch-owned/result.json):

```text
main:   success   stop=end_turn               claim_remaining=true  marker=finished
main:   cancelled stop=cancelled              claim_remaining=true  marker=running
main:   failed    stop=RequestError:Internal  claim_remaining=true  marker=running
branch: success   stop=end_turn               claim_remaining=false marker=finished
branch: cancelled stop=cancelled              claim_remaining=false marker=running
branch: failed    stop=RequestError:Internal  claim_remaining=false marker=running
Both: ATTACH_AND_DETACH_DID_NOT_CANCEL for all three turns.
Both: PASS_REAL_PI_ACP_JOURNEY
```

The injected child death reports `Native tool outcome remains UNKNOWN`, as it must. Releasing a cooperative file claim does not authorize replay or deny prior side effects.

After merging current main and removing the unnecessary SQL open, repeated one fresh **cancelled** case only on final source (`--outcomes cancelled`, final-cancel/result.json). Actual configured Pi wrote the file, entered bash's 30-second sleep, survived the second ACP attach/detach, then explicit ACP cancel returned `cancelled`, with **claim_remaining=false**. PASS_REAL_PI_ACP_JOURNEY. No earlier UNKNOWN input was replayed.

## Sanity checks and limits

`PYTHONPATH=src timeout 300 /home/ts/code/projects/agent-comms/.venv/bin/python -X faulthandler -m pytest -o addopts='' -n 0 --no-cov -q tests/test_ordinary_native_claim.py tests/test_envelope_claim_transitions.py`: **13 passed**. The ordinary-write check no longer monkeypatches CodingToolOwner.allow or asserts that terminal claims survive.

A final serial batch after main integration passed **15** claim checks; its one ordinary-native fixture failed *before sending input* because native get_state preflight exceeded 10 seconds (zero localhost provider posts, zero input proofs). Available RAM had fallen to 3.6 GiB, with 15.8 GiB swap used; no cap was raised and that attempt was not retried. The separate final configured-provider cancellation journey then passed. This is not a clean full-suite result.

The earlier six-test batch was not a passing suite: three selected-release tests reached an already-deleted `HistoryViews.full_history_page` API after successfully clearing claims; the existing real worker stop/start fixture timed out awaiting its later edit. Its run is retained under `failed-focused-run/`. Default whole-package coverage also rejected the narrow batch. No fake-based disposition test is claimed as acceptance. These failures are not used to explain or design the fix.

The first setup attempts stopped before the intended operation: missing participant bootstrap, unsupported off thinking, and foreign scratch destinations. They are retained separately; the latter's actual Pi response confirms both tools were refused, rather than inferring no effects from a failed call. The final driver uses the real launch preparation and declared StorageRoots. No UNKNOWN attempt was replayed.

Scratch native artifact: copied the existing reviewed native package into this checkout's ignored `stack/.pi-native-a0f57a811b47862f` for the source runtime; removed after real borrower inspection and owner shutdown. No installed package, live launcher, or restart queue changed. The actual source checkout was supplied outside `/home/ts/wt`; attempted relocation was denied by the enforced storage rule, so it remains at its assigned persistent path. The failed default pytest run was moved from its generated /tmp path to the owned persistent `failed-focused-run/`, preserving its sessions. Virtualenv creation was also denied; all work used shared interpreters.

## Assumed

- The owner's statement that no intentional cancellation occurred is accurate; the retained wire does not establish the UI action that emitted cancel.
- Abrupt ordinary-owner death without stream cleanup is outside demonstrated terminal cleanup; ordinary claims still have no durable turn binding. Selected-attempt death recovery retains its existing durable release.
- Source runtime + real ACP/native verification is not an installed Toad UI journey. There is no verified spontaneous-attachment cancellation and no speculative cancellation-policy change in this PR.

## Checklist

- [x] Inspect incident ACP wire and exact claim projection.
- [x] Implement ordinary resource retirement and share exact-generation release.
- [x] Release all nine original claims and obtain real history.py edit confirmation.
- [x] Attach and disconnect a second stdio ACP client during real 30-second Pi bash operations on main and branch. Neither cancels.
- [x] Confirm success, explicit cancellation and native-child failure release through the real native path.
- [x] Focused checks: ordinary native write and claim transitions, 13 passed.
- [x] Final standalone debt ratchet passed, exit 0, against origin/main 011084fc5 and branch b33e32b7c. Zero positive deltas and zero introduced findings (`ratchet-final-integrated.log`). Initial report found two missing new annotations, two new local imports and a 21-line source increase; annotations/imports are repaired, unused SQL work deleted, and this feature's 21 net source lines declared through the existing Src-Budget commit trailer. No semantic-debt allowance is used. A concurrent main update initially made unrelated commander files appear changed; integrated main normally, then ran the required standalone check again.
- [x] Record remaining trigger uncertainty accurately; do not manufacture an attach failure.
