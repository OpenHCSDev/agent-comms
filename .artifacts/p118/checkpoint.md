# P118 working checkpoint — NOT budget acceptance

Owner: toad-theme. Checkout /home/ts/wt/core-p118-owner, local branch fix/p118-shared-child-owner tracking origin/fix/p118-pi-event-owner (draft PR1161).
Head: 902e5ad9ce88916e1fca70d0cd1c0c0afb99194a.
Inherited base for this assessment: 0ec36f573c7e4b4f0be6fdcf382e54efdb0eec46.

## Source ownership
- PiChild retains shared child retirement and resources; native key/attestation remain PiSessionChild.
- Native TurnSession keeps its original watchdog receiver. Removed the newly introduced native producer task/queue and PacketRead/WakeRead parallel transport family.
- PiRpcChannel owns Durable's opt-in streaming task, event queue and request correlation. `_read_task` is declared only there. Its queue shutdown wakes EOF/failure readers without allocating a task for each next_event.
- Native BorrowedNative uses its original pending response capability; Response.apply resolves that original request through the existing channel. Native idle and Durable requests share child failure/resource handling.
- NativeCustody owns common prepare-then-inspect behavior. Borrowed and retiring states override the capability they actually own.
- Native MessageEnd and Durable's required stored-entry envelope remain distinct codec shapes. Shared ToolExecutionEnd no longer declares an unused stored-entry mirror.
- Strict duplicate-field JSON decoding now uses JsonValue, not three Any annotations.
- CLI check continues to prohibit backend, tracked_turn and turn_watchdog loading. It no longer prohibits the shared custody declarations now imported by backend metadata.

## Results
- Before/after NRA Repository/Package AST census: 332 Python modules, zero parse omissions; sites retained in ast-before.json and ast-after.json.
- SDK JavaScript event producer was read semantically, not parsed by the Python audit; no JavaScript was edited.
- Final bounded batch: 75 passed, 12.08 seconds (check-3.log). Includes actual SDK host context/error commands, turn watch/configuration/reopen, held-writer history, CLI import subprocess, and the actual native tool-effect terminal-failure path. No live-provider input, installed-default or visible UI acceptance claim.
- Initial check-1.log: 74 passed / one native StreamSettled failure after removing the extra receiver. Corrected the original request correlation in Response.apply. Targeted follow-up check-2.log: 2 passed. Final batch above includes both.
- native_custody.py: +45 / -50, net -5 against assessment base.
- DurableHost declaration count: 0.
- Ratchet exit 1: ONLY SourceLines remains positive (+137 over the existing 400 allowance), actual source net +537. No other introduced measure. This misses the requested <=+400 and is NOT ready for acceptance or merge.
- Model/configuration and history declarations have NOT yet received the required reduction. transcript_receipts.py remains +34; native_entries.py remains +22.
- Core InputId/send-disposition worker integration remains explicitly unfinished and opt-in; no claim that Durable worker submission works.

## Retained paths / borrowed resources
- /home/ts/.cache/agent-scratch/p118-toad-theme-20261017-owner-1: failed bounded generated SDK/native check, preserve raw failure.
- /home/ts/.cache/agent-scratch/p118-toad-theme-20261017-owner-2: focused correction check, new generated root; no replay.
- /home/ts/.cache/agent-scratch/p118-toad-theme-20261017-owner-3: final bounded generated SDK/native check, new root; no replay.
- src/agent_comms/node_modules is a symlink borrowing /home/ts/wt/core-native-runtime/src/agent_comms/node_modules unchanged. No npm install or installed-package changes.
- No viewer, worker, or deliberately retained SDK process. Original live bus, saved sessions, UNKNOWN attempts and other owners' checkouts untouched.

Next work: reduce the remaining model/configuration/history declarations through their actual existing owners, without moving code merely to meet the allowance, dropping behavior, or changing stored encodings. Need another 137 source lines removed for budget acceptance.
