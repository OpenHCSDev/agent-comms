# S4 Part B: current installed mounted read receipts

PR307 continuation, synced with main `b77db612`. Installed production bytes:
core `664623a3c1393c878a0dbf59e65eb2e530163cd9`, Toad
`436514d875c01964f3a4fea6919f1892e60c73f8` (140), Textual
`c9743801c98dc570f82f82e25915ecce89800f4b`.
`installed.json` records imported paths and distribution provenance. All pilots
use the immutable live-installed interpreter, with their own wire, config,
state and data directories under this worktree's persistent `.artifacts`.
No live wire writes, prompt dispatch, provider request, or production edits.
The extra `native_attach.py` check starts/stops only its copied test-owned
executor. Original source size/mtime are checked unchanged; its 39 MB private
copy and configuration are removed after owner teardown.

## Binding requirements and actual acceptance

| Original S4 Part B obligation | Executed current path |
| --- | --- |
| Display basis reflects painted messages, not fetched pages | `dm_paint.py`: compositor contains FIRST_DM_PAINT_RECEIPT and actual painted key; durable human read advances. Switch away, append and fetch new DM: it remains unread; refocus advances read. |
| Channel projections/read ACK while scrolling and switching | `channel_follow.py`: reuse Carver's maintained channel follow pilot rather than invent another matrix. Initial bounded page leaves older messages unread; late message is confirmed in compositor, roster resize preserves follow; scrolling up preserves position; return to tail/refocus acknowledges. |
| Executor delivery remains distinct | Channel pilot acknowledges a registered executor while the hidden human view retains two unread messages, then mounted refocus clears only the human unread view. |
| Transcript human-read fact follows native painted cursor; publication alone is not visibility | `transcript_paint.py`: actual native JSONL producer/parser -> TranscriptSnapshotUpdate -> canonical encode_updates/decode_updates -> installed Toad dispatcher/publication -> cropped compositor text and durable ReadLedger. Publish a later native reply offscreen: unread remains one; return to native view/tail: unread becomes zero. No mocked parser, ledger, publisher, widget, or ACK. |
| Fresh installed native/ACP affected path | `native_attach.py`: copy actual retained NRA source, declare current native package on private wire, real installed Toad Agent/ACP initialize/load/native owner, nonempty saved publication and mounted durable native read. 25 retained events, history height147, cropped paint2070 characters; zero prompts. Actual transport is used here, unlike the direct publisher seam check. |
| Mode expansion / DM replacement / crash reopen | The maintained 40-check installed receipt in the preceding PR307 commit covers these backend predicates. This mounted batch does not duplicate their randomized matrix. |
| OPEN8 Toad marker key authority | Pilots invoke installed UI and its production ACK path. They do not generate marker keys or write a second read store. |

The transcript pilot constructs the actual Agent as a publication dependency,
without starting a backend transport. Its external native records are small
test-owned files. This proves the installed file/codec/UI/read-receipt path;
it does **not** claim a fresh ACP attachment or selected-provider exchange.
The separate `native_attach.py` supplies fresh ACP/native attachment acceptance;
no selected-provider exchange is claimed. Parent's four actual attachments and PR95 retained paint remain
separate evidence. Carver141 was contacted on its PR; this batch uses no retired
root cursor property and can run unchanged against its publisher ownership API.
No Carver, Noether, or parent reconnect product surface was edited.

## Commands/results

```
runtime=/home/ts/.local/share/agent-comms/runtime-watchdog-20260928/bin/python
$runtime evidence/s4-mounted-read-ack/channel_follow.py
$runtime evidence/s4-mounted-read-ack/dm_paint.py
$runtime evidence/s4-mounted-read-ack/transcript_paint.py
$runtime evidence/s4-mounted-read-ack/native_attach.py
```

All four passed; raw outputs retained in `channel-follow-paint.log`,
`dm-paint.log`, `transcript-paint-final.log`, and `native-attach-diagnostic.log`.
First channel attempt asserted
before asynchronous history publication; bounded publication wait fixed the
test, not production. First transcript attempt lacked an Agent, so the real
publisher correctly refused its snapshot; final uses the actual Agent and
canonical codec. Failed receipts are retained rather than called green.
First fresh attachment attempt queried an unmounted Conversation; cleanup
exposed a sidebar exception. Subsequent attempts selected the first empty
control history rather than the nonempty source publication. Final test waits
for current mounted source events and confirms that publication's geometry.
These test setup mistakes required no production changes. Ruff passes all four
pilots. Carver141 coordination/correction/results are recorded on that PR.

Remaining unproven: certified NRA detector inventory/coverage, historical
retired-consumer equivalence, and universal/full-suite claims. None is a CI hold.
This receipt closes the named current mounted ACK obligations, not all possible
display permutations or post-141 source-rebind semantics owned by other agents.
