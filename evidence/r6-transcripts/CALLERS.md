# Canonical current callers

| Removed usage | Current owner |
|---|---|
| `from agent_comms.transcripts import TranscriptEvent` | `agent_comms.transcript_events` owns root and concrete declarations |
| `TranscriptEvent("assistant", text, ...)` | `AssistantTranscript(text, ...)`; likewise User/Thinking/Context/Notice/Sent |
| `TranscriptEvent("tool_start",...)` | `ToolStartTranscript(tool_call_id=...,tool_name=...,raw_input=...)` |
| `TranscriptEvent("tool_end", text,...)` | `ToolEndTranscript(tool_call_id=...,tool_name=...,text=...,ok=...,diff=...)` |
| `event.kind` rendering/category switches | declared MRO handlers on actual types |
| unconditional `event.text` size budget | `event.text_size` |
| `is_routed_event(event)` | `event.routed` |
| `TranscriptEvent.from_wire` / `event.to_wire` | `TranscriptCodec.decode(TranscriptEvent,data)` / `.encode(event)` |
| raw snapshot cursors/optional missing-page fallback | `TranscriptCodec.decode(TranscriptPage,{**metadata,"events":facts})` |
| `TranscriptUpdate.from_transcript` | `event.replay_update()` |
| `Transcripts.record_input_display` | `Transcripts.routes.record_input_display` |
| unread callback parameter | `TranscriptReadState.counts(viewer,sources)`; NativeEntry owns reply meaning |
| raw session role/content/model entry parsing | `NativeEntry` and `NativeTranscript` using R1 payloads |
| `TranscriptRoutes(old_json_path)` | `TranscriptRoutes(root)`; canonical database_path |
| live old-writer JSON revisions/source field | one-way transactional import; indexed-only current authority |
| transcriptDiffs/diffs negotiation | deleted; current snapshots include actual diff evidence |

TranscriptCursor/Page remain in transcripts.py. Core source edits are limited to the listed files; no source changes in coordinated_runtime, native journal/commit, broker/claims or production settings. Toad paired source owns all current transcript render/fragment/category/ACP consumers; parent pins it together with this core.
