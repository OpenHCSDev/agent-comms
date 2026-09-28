# R1 direct caller and deletion map

| Owner | Current callers / replaced surface |
| --- | --- |
| PiEvent + PiPayload/FieldCodec | backend, native_pi, manual_compaction, selected_pi_route, selected_pi_summary_rpc, pi_rpc; typed attributes replace Mapping/get/wire and repeated nested JSON shapes. UnknownPiEvent.payload is external opaque content only. |
| PiCommand.response_payload / Response.command | preflight, discovery, correlation, settlement, manual and selected requests; command class owns schema. Deleted Response.command_type. |
| PiMessage/PiContent/PiDelta | pi_events, native_pi, turn_inputs, selected_tool_broker, channel_coding_tools; native text/tool content and forwarding input IDs decoded once. Delta declarations emit behavior. |
| PiUsage/UsageAccount | pi_events, agent_events.ProviderUsage, turn_usage, turn_progress; typed values internally, FieldCodec external JSON only when recording provider usage in existing GoalAttemptStore. Deleted UsageAccount.positive_tokens adapter. |
| PiToolResult | pi_events → ToolDiff; transcripts._events explicitly decodes the saved external tool-result boundary. Parent paired Toad uses unchanged ToolDiff/TranscriptEvent, no Pi direct caller found. Extension details are opaque; edit detail interpretation stays ToolDiff. |
| ImageInput | Prompt decoding and backend outbound native images reuse existing validated owner. No raw nested image mirror. |
| SelectedSummary/Probe/Settings payloads | selected_pi_route, selected_pi_summary_rpc, typed AgentComms* commands; existing NativeWitness/PiCompactionDecision reused. Exact correlation/proof/grant ownership unchanged. |
| Current R5 activity journal | tests/test_acp.py inspects persisted state trail; removes final test-only ActivityLog._load caller without restoring a removed API. |

Only known required wire fields are projected into typed owners; no second raw copy. Native saved entries/proof files and journal transaction readers are separate original R6/R7 facts, not Pi RPC mirrors. No production R3/R5 files changed.
