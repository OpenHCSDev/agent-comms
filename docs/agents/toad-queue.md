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

Actual installed Toad ran in real `st` on owned Xvfb `:171`, with the configured `openai-codex/gpt-6.1-sol`, and prompts were physically typed into Toad with xdotool. No `agent-comms send` inputs were used. The baseline used Core main `8b27e02a`; the branch used Core `a6372b90f` from this checkout, with the same installed Toad/dependencies and shared interpreter. The worker and paired ACP/frontend imported Core from this checkout's `src` working directory. No live installation or existing user session changed.

| UTC capture | Actual result |
| --- | --- |
| 2026-10-19 17:33:46.832, main | Original `QUEUE-INITIAL-MAIN-2` is already in conversation history, `Run sleep 60` is running, but `Queued (1)` still contains that original. |
| 2026-10-19 17:38:33.007, branch | Original `QUEUE-INITIAL-BRANCH-ACTUAL` is in history, `Run sleep 60` is running, and the queue area is absent. |
| 2026-10-19 17:39:17.380, branch | Two subsequent messages physically typed while the tool ran show `Queued (2)`, containing only the two undelivered follow-ups. |

Exact decoded `queue_changed` facts from the real ACP logs, each immediately after its original `input_started`:

Main, line 58 (original start was line 57/revision 12):
```json
{"kind": "queue_changed", "scope": {"session_id": "queue-main", "admission": {"incarnation": {"name": "queue-main", "created_at": 1792430448.4388702}, "admission_generation": 5}, "owner_pid": 973228}, "revision": 13, "projection": {"kind": "available", "items": [{"input_id": "5f1dc7e26fdc4bde86e60dc3a3190874", "text": "QUEUE-INITIAL-MAIN-2: Run bash command sleep 60; then run bash command sleep 60; then reply DONE. Do not inspect files or call comms tools."}], "restored": []}}
```

Branch, line 20 (original start was line 19/revision 6):
```json
{"kind": "queue_changed", "scope": {"session_id": "queue-branch", "admission": {"incarnation": {"name": "queue-branch", "created_at": 1792431394.062381}, "admission_generation": 2}, "owner_pid": 1007955}, "revision": 7, "projection": {"kind": "available", "items": [], "restored": []}}
```

The branch ACP log starts the original at line 19/revision 6 and publishes an empty queue at line 20/revision 7. After the first sleep, it starts the follow-ups individually, publishing counts 1 (line 35/revision 11) and 0 (line 38/revision 13), then runs the real second `sleep 30` tool and responds `DONE` (line 45). A physical intermediate count-1 frame was not captured: Pi batches steering. The later `screen-zero.json` capture is after completion, not before turn end; only the original-start screenshot is claimed as a physical zero-before-end proof. Both real tools succeeded, confirming that removing the consumed queue row does not revoke their running turn.

Physical evidence and original sessions remain under `/home/ts/.cache/agent-scratch/pi-toad-queue/`: `original-main/{screen-retained.json,initial-still-queued.png,queue-events.json,state/toad/logs/}` and `original-branch/paired/{started.png,screen-two.json,queued-two.png,queue-events.json,state/toad/logs/}`. The first run used a task-started turn and exercised only following inputs; it did not close the initial-input defect. A rejected mixed-Core attachment sent no prompt and is retained separately. Source syntax and `git diff --check` pass. No automated tests, fake protocol events or synthetic application were used. Owned frontend/native worker/display processes were shut down after the completed turn; original sessions and raw failures are retained.

Identity search after: `rg -n '^    input_id:|def accepted_id|accepted_input_id=self\\.' src/agent_comms/queued_input.py` finds only the accepted `input_id` field and its dispatch, no alternate accepted-ID properties. `AcceptedInputSourceRule` and dispatch's `require_live_source` both use the queued input's `is_live` behavior.
