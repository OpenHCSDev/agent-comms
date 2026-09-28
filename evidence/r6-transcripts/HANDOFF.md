# R6 saved transcript boundary — complete source handoff

Owner Pascal. Core tree `~/wt/comms-refactor-r6-transcripts-20260928`, branch `codex/refactor-r6-transcripts-20260928`; stable source `67e1974951b8f4aeff38ecd4b3567caf639d5e80`. Includes main199 R3 and integration198 (`ee632a9`). Paired Toad tree `~/wt/toad-refactor-r6-transcripts-20260928`, branch `codex/refactor-r6-transcript-consumers-20260928`, source `38b779984727561bddfe7e0a2983b51b710420a7`; includes main97. Later commits add only this receipt/caller map.

## Complete ownership and deletion

- `NativeEntry` reuses R1 `PiPayload`/`PiMessage`/content owners. Native message, compaction and model-change entries decode once; unknown external records remain opaque. `NativeTranscript` owns bounded forward/reverse/tail byte traversal; original page cursors, partial tails, frozen through-window and oversized-record rules remain.
- R1 message/content declarations own saved presentation behavior, including adjacent text, images, thinking, error notices, original input text/provenance, tool start/result/diff and verified sent messages. Native files remain authoritative and unchanged.
- Variant-specific `TranscriptEvent` declarations replace the kind/optional-field bag. `TranscriptCodec` derives the snapshot shape and composes the existing richer Message boundary inside routing. Page metadata derives from declared fields without serializing the event payload a second time.
- Pages, tails, model inheritance, unread-index classification, route annotation, replay, runtime subscribe and all current core tests use the actual owners. Existing reply index/read ledger remain in place; no second index or native authority.
- Paired Toad renders, categorizes, fragments, continues live text, processes worker render jobs, and decodes complete ACP snapshot pages using those same declarations. Shared MroDispatch now also supports synchronous consumers from the same handler declarations; no new registry or role roster.
- Removed old `TranscriptEvent(kind,...)`, `.kind`, `.to_wire/.from_wire` adapters, `Transcripts._transcript_record_events`, `_transcript_message_events`, the `Transcripts.record_input_display` forwarder, `HistoryViews._is_unread_reply` callback, redundant Assistant/Notice/SentTranscriptUpdate subclasses and `TranscriptUpdate.from_transcript/owner_for` string dispatch. Removed transcriptDiffs/transcript_diffs/diffs old-client negotiation; current snapshots always retain actual tool diff evidence. Standard ACP text replay remains a real protocol path.
- Removed route JSON revision watching, `_legacy_revision`, `.path`, dual live writer source projection and the running-old-writer test. One transaction imports actual saved annotations once, keeps indexed rows authoritative on collisions, drops `routes.source` and retires the old revision marker. Original JSON stays archival and is never reread after migration. Rollback on invalid original data is covered. No old-client coexistence gate or compatibility aliases.
- Paired Toad removes string role dispatch and `is_routed_event` forwarding; replaces `TRANSCRIPT_ROLE` with actual event declaration ownership. The ACP adapter no longer invents a page when required current snapshot metadata is missing. All existing producers/fixtures migrated.

## Local acceptance and actual strength

- `core-current-main.log`: 232 passed, 14 failed initially. All 14 resolved: one decode-count test now accounts for the existing one-record lookahead; eight R3 fixture failures needed the newly used InputDispositions import after merge; five socket tests used an overlong owned fixture prefix. `seam-repair.log`: all48 passed with short persistent fixture root. Together these cover all246 cases in that affected batch; the failed initial receipt is retained, not called green.
- Coverage includes native entry/current family roundtrips, discriminator separation, malformed/unknown/partial records, forward/reverse decode-once, pagination/forks, input provenance and repair, route migration/rollback, persistent unread index, ACP replay/configuration, runtime socket, live saved tool diffs, Pi RPC/content, and operation/reply-routing consumers.
- `native-stack-matching.log`: 2 passed, actual prepared native Pi + local HTTP fake provider, single and batched channel input receipts. No paid provider. The initial `native-stack.log` used the older 20260927 launcher and timed out after failure markers; matching existing 20260928 launcher has the current commitment and passes. No package copy/install or native patch.
- `saved-pages-current.log` / `saved-pages-result.json`: all88 original saved sessions' bounded latest/prior event facts AND cursors equal R1 baseline after current R3 merge. Read-only original sessions; route/registry writes only in owned copies. Parent accepted this scope; full original route-table preservation/mounted copied roots are parent-owned.
- Paired Toad receipts: actual process/stale/cancelled/detached/scroll-intent pilot, mounted tool diff, all7 categories, lazy coordination context and native adjacent message parts pass. The last two rerun after complete typed snapshot page decoding (`context-page-boundary.log`, `native-message-page-boundary.log`). Main97 adds four unrelated delivery fixture migrations/pin; retained intact. Early syntax/fixture failures retained with corrected receipts.
- NRA selected seven source files with full package context: 79 detectors, zero omissions, complete exact_compact_global, zero findings. This is coverage, not blanket behavioral/equivalence proof. Changes are authored ownership migrations; no native DSL equivalence claim.

## Commands

Core: `PYTHONPATH=src TMPDIR=/home/ts/wt/.r6-test timeout 60 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -q -o addopts='' <affected tests>`; exact case collections in receipts. xdist/coverage defaults disabled.

Actual local Pi: same Python with `AC_NATIVE_STACK_BIN=/home/ts/wt/comms-refactor-integration-20260928/stack/bin/pi-native`, the `[deliver]` and `[batch]` nodes of `tests/test_stack_channel_delivery.py::test_native_channel_input_receipt_and_revocation`.

Paired Toad: `PYTHONPATH=src:/home/ts/wt/comms-refactor-r6-transcripts-20260928/src TMPDIR=/home/ts/wt/.r6-test timeout 60 /home/ts/.local/share/agent-comms/runtime-r1-pi-payloads-20260928/bin/python tests/<pilot>.py` in its own Toad tree. Existing environment reused.

Exact successful full-context NRA invocation (18seconds, bounded):

```sh
timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor src/agent_comms/native_entries.py src/agent_comms/native_transcript.py src/agent_comms/pi_payloads.py src/agent_comms/transcript_events.py src/agent_comms/transcripts.py src/agent_comms/transcript_routes.py src/agent_comms/transcript_updates.py --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload loop
```

## Integration boundary / remaining scope

No known unfixed R6 source failure remains. Parent owns whole-route migration comparison on original-root COPIES (59465 routes/5617 displays/5408 input routes plus live11/11), copied-root mounted history, paired core pin and serial activation. Idle old writers before live migration: schema drops source and old snapshot clients are intentionally unsupported. This is not installed/live R6 acceptance. No live root touched, provider called, owner restarted or CI gate imposed here.

Darwin R3 `repair_input_routing` retains typed InputDocument/InputAttempt semantics; only route owner call changed. FieldCodec `family_discriminator` is separate from `PiPayload.wire_tag`, with a regression case. R7 is Darwin's; no coordinated-runtime edits in this branch. Parent relays any narrow seam.

`CALLERS.md` contains canonical mapping; `CHANGED-FILES.txt` contains exact source/test paths relative to current main. Original saved-page fixture copies and exited test caches are disposable; only scripts and concise receipts are retained in the PR.
