# P130 durable tool-start conversion

Product source is unchanged from accepted `0da2e8177`. The converter checkpoint is `55561324fed9de4777fe480f01f12f3d22e02b42`, draft #1154. This is not live conversion or installed acceptance.

## Actual holders and consumers

- `src/agent_comms/transcript_events.py:205`: `ToolStartTranscript` owns canonical raw input and derives its title; `detail` is no longer a declared field.
- `src/agent_comms/cli_bridges/shared.py:48-63`: `ToolCall` is transient. `finished()` constructs the start/end pair; its diff stays on the end event.
- `src/agent_comms/cli_bridges/claude_protocol.py:113-132`: `ClaudeSession.tools` retains calls in memory until results; it is reconstructed from external Claude records, not serialized as ToolCall.
- `src/agent_comms/cli_bridges/codex_protocol.py:345-348,628-629` and `cli_bridges/opencode_parts.py:146-148`: other transient ToolCall producers. No durable ToolCall field was found.
- `src/agent_comms/external_transcript.py:29-37`: **ExternalItemRow.events** is the durable finished-event tuple. Keys are thread/turn_id/item_id; seq retains position.
- `src/agent_comms/external_transcript.py:58-64`: **ExternalDraftRow.events** is the durable growing-event tuple. Its key is thread.
- `src/agent_comms/external_transcript.py:166-173,186-208`: ExternalTranscript owns the database filename and complete row declarations, and owns finish/draft/end-turn writes. The converter derives both table names, columns and primary keys from those declarations; it does not maintain another holder roster.
- `src/agent_comms/cli_bridges/shared.py:300-332,670-698`: FinishedItem and DraftItem are transient publication messages; ThreadOutput sends them through the existing ExternalTranscript writer.
- `src/agent_comms/external_transcript.py:67-75,117-149`: ExternalRecord and TurnDigest are transient read results from those same rows.
- `src/agent_comms/transcript_events.py:53-56,237-241`, `transcripts.py:93-110`, `turn_digests.py:19-45`, `acp_extension.py:506-517`: coverage/page/turn/snapshot carriers, not additional saved event stores.
- `src/agent_comms/pi_payloads.py:312-325` and `native_transcript.py:321`: native starts are derived from original native call records, not saved ToolStart objects. Native journals are unchanged.
- `src/agent_comms/transcript_receipts.py:141-157`: page boundaries consume start/end types; they do not store another copy.
- Paired Toad consumers: `src/toad/acp/status.py:50-69`, `widgets/transcript_fragments.py:522`, `widgets/cli_draft.py:21`. These prepare/view the Core-owned events. Toad's ACP ToolCall and widget ToolCall names are separate classes, not additional saved Core ToolCall holders.
- `src/agent_comms/read_ledger.py:33,35`: legitimate paired-key dictionaries remain untouched (RecordedThreadIncarnation/Conversation and transcript tuple keys). Bridge DeliveryRow.payload/ObservedPart.body retain messages/monitor text, not typed ToolStart objects (`cli_bridges/shared.py:462-486`).

AST evidence: `.artifacts/p130-tool-start-before-ast.txt` and `p130-holder-ast-sites.txt`. Corpus includes Core src/tests/tools, paired Toad and installed metaclass-registry:1069 modules, zero omissions. Dynamic registration, arbitrary getattr and callback bindings cannot be exhaustively proven by syntax; the explicit stored declarations and executable writers/readers above were read semantically.

## One stopped conversion, no compatibility reader

`tools/cutover/carry_tool_start_detail.py` opens the source read-only, pins its SQLite snapshot and backs it up into an exclusively created output. It removes only direct ToolStart.detail fields in the two declaration-owned event columns. Nested raw tool arguments are not walked. All identities, sequences, event order, routing, times, raw inputs, result text, diffs and other table/column values remain intact. Every output row is strictly decoded using the actual target TypedTable/FieldCodec owner. Malformed remaining data aborts, rather than becoming an unknown-field-tolerant reader. An existing output is refused.

The operator must stop the original producers/clients using the existing cutover, run this tool with the reviewed target interpreter, preserve the stopped original, then publish the successfully verified output through that same cutover. Codex alone does this. No new stop/launch algorithm or startup converter was added. Delete this disposable tool after the durable cutover.

## Complete copied-store qualification

Codex confirmed native-stall-0438 contains an extracted failing event, not a frozen DB. A read-only SQLite backup of the actual live external store was therefore taken into `/home/ts/.cache/agent-scratch/core-goal-retry-p130/original.sqlite3`; source connection changes:0. No live conversion or provider input occurred. Under the measured resource pressure this used bounded existing-dependency jobs, not another environment, agent fleet or broad suite; the two retained transcript copies need about850MiB on /home.

`.artifacts/p130-copy-proof.json` records the independently compared result:

- **80,540 finished rows, all strictly decoded at the accepted0da source.** Draft count0, preserved0; the declared draft table is included, but a nonempty real draft was not present in this snapshot.
- **137,058 events**, with IDs, tuple order and every retained event field compared.
- **4,897 retired details** removed across4,868 rows. No other JSON field difference is allowed by the comparison.
- **56,261 starts;60,800 ends;589 diffs**, all preserved apart from the specifically deleted start detail.
- Codex's exact failing retained event matches **one** original row.
- Complete schema matches both the source and the current ExternalTranscript declaration (`p130-schema-input-state.json`). All non-event columns and all other table values match.
- The frozen original and the separate **8,602-row input-disposition snapshot** have identical before/after SHA256 values. This includes every UNKNOWN row; no input is settled or replayed.
- A Python SQLite audit records only the read-only original connection and the new output connection during conversion. The input-disposition store is not opened by the converter.

The canonical result is `/home/ts/.cache/agent-scratch/core-goal-retry-p130/qualified.sqlite3`. Raw first-run results, comparison script, read-only backup result and failures are retained in `.artifacts/p130-*`. Originals, sessions, live stores and UNKNOWN inputs remain untouched. These are copied-data and real decoder checks, not a claim that Codex has executed the stopped live migration.
