# T2 paired ACP extension ownership

Comms base229e6d7fe08; Toad base1071d8bf470 (already incorporates T1/T7/T8/TL0A). Owner dispatched T2 after229 integration; CI/old mocked suite remain asynchronous and do not hold deployment. No live install or provider calls in this branch.

## Existing owners and coordination

- Darwin owns T1 settings and final native package rebuild. Direct coordination and exact selected-tool mismatch: https://github.com/OpenHCSDev/toad/pull/119#issuecomment-5877151470
- Copernicus owns107 integration/current typed Goal/process callers: https://github.com/OpenHCSDev/toad/pull/107#issuecomment-5877151732
- Nietzsche owns this entire paired T2 boundary, producer/client/caller/test deletion. Parent owns paired cutover/install. No duplicate family/codec/store.

## Required source answers

1. `InputDrain.queue_state` already projects complete current+restored queue items, exact IDs, admission scope and monotonic revision. `QueueReducer` adds attachment-request buffering, stale response floors and local display echoes. Queue reconstruction belongs with producer; attachment request freshness remains with client attachment ownership. UNKNOWN never creates a queued input or retry.
2. `SessionLifecycle.metadata` carries goal/execution, coordination, queue and cursor at once. Error updates carry route plus failed input. Therefore one list of typed facts is necessary.

## Full closure scope (not yet complete)

- One `AgentCommsUpdate` family in `acp_extension.py`, encoded by existing FieldCodec, class-derived tags. Toad imports exact declarations and dispatches with existing MroDispatch.
- Producers: agent_event_updates, transcript_updates, input_drain, session_lifecycle, runtime, acp, manual_compaction_bridge, compaction_publication, turn_progress; request/control counterparts included.
- Consumers: ACP Agent extension decoder/component, messages, Conversation, MainScreen/ToadApp coordination facts, queue_view/private_native_cursor and their callers. Keep external ACP discriminator/schema intact.
- Delete copied UI payloads, raw extension-key probes, capability/version-skew branches, epoch vocabulary, queue/cursor hand decoders/reconstruction and obsolete pilots. Shared existing Goal/ThreadIncarnation/TurnIdentity and lifecycle owners remain authoritative.
- State classification: extension and attachment observations are transient. No durable history bytes changed; no converter or data reset in source.
- Acceptance: family/new-case declaration extension, recorded actual producer through mounted Toad consumer, affected real ACP/native path, permanent guards, paired pins/install. No claim of surface completion before full caller/deletion and paired activation closure.

Initial implementation migrates turn lifecycle, text route, transcript invalidation and backend failure observations together; remaining queue/cursor/coordination/goal/compaction/MCP/request facts stay explicit T2 work, not delegated or declared done. Neither draft is deployable independently.
