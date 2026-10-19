# Drain accepted initial inputs when Pi starts them

The live failure is in Core, not Toad. `InitialInput.run` captures and queues a real `input_id`, but `InitialInput.accepted_id` replaced it with `None` when dispatching. `StartedInput.started` consequently published the correct durable public ID and removed `queued[None]`. Its queue publication still contained the original. Ordinary follow-ups already retained their ID.

Observed in the owner's original `Agent_Comms_2026-10-19T13_08_20_015597.txt`: line 22 starts `3a6d4d9d80244defaf7f03866b6dfd43`, line 23 still queues it. In a new real installed Toad run: `original-main/queue-events.json` records initial start revision 5 followed by revision 6 containing that same input, follow-up start revision 8 followed by revision 9 still containing the initial, and only revision 10 empty at turn end.

## Ownership change

- `QueuedInput.input_id` is the accepted identity for initial and following dispatch alike. Delete both `accepted_id` aliases, including the `None` override. No fallback or Toad-side queue subtraction.
- The existing queued input classes own live membership: an initial is still the exact queued object; a following input still has its captured following source. `require_live_source` and `AcceptedInputSourceRule` now consume that shared behavior.
- Queue acceptance grants the native send. It is checked in `OwnedSend._prepare`, not again after native start when a tool verifies its running turn. `verify` still requires a started native input, trustworthy output, the exact owner/turn, source keys, goal and wait authority. Keeping the consumed queue row just to authorize later tools would recreate the bug.
- `StartedInput.started`, queue events, scope/revision validation and Toad consumers remain unchanged.

AST inventory before editing covered 662 Core and Toad production Python files, with no parse omissions. It enumerated the identity declarations, queue-membership checks and all consumers; dynamic aliases were not resolved. Relevant source: `queued_input.py` (`QueuedInput`, `InitialInput`), `owned_turn.py:reserve_input/open_stream`, `owned_send.py:_check_owner/_prepare/verify`, `ordinary_turn_rules.py:AcceptedInputCheck/AcceptedInputSourceRule`, `started_input.py:started`. This repairs catalog IDEN-6 (wrong identity) and removes the two independently maintained membership checks.

## Verification

The owner's installed Toad 0.6.20 has 313 tracked files, no extra non-bytecode files, and differs from Toad main `0261082e` only in `widgets/prompt.py` danger highlight styles. Queue code matches.

Physical evidence and original sessions are retained under `/home/ts/.cache/agent-scratch/pi-toad-queue/`; the first run used a task-started turn and therefore exercised only following inputs. That passing path did not close the initial-input defect. No automated tests, fake protocol events or synthetic application are used. Branch physical acceptance is pending at this checkpoint.
