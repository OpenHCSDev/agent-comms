# ACP exact-ID queue projection v1

This is the authoritative **read-only presentation** of the currently attached
ACP owner's in-memory deferred-display queue. It is not a Pi acceptance,
consumption, ACK, retry, completion, or native cursor. Durable UNKNOWN/STARTED
input dispositions remain independent. No exact-ID edit/remove/reorder API is
promised. A crashed owner may leave UNKNOWN input evidence even when this
volatile queue snapshot is empty; never automatically replay it.

Trusted `session/new`, `session/load` and owner-socket ready include:

```json
{
  "queueBinding": {
    "version": 1, "sessionId": "beta", "ownerThread": "beta",
    "ownerCreatedAt": 1000.0, "ownerEpoch": 3, "admissionGeneration": 3
  },
  "queueState": {
    "version": 1,
    "scope": {"sessionId": "beta", "ownerThread": "beta", "ownerCreatedAt": 1000.0,
              "ownerEpoch": 3, "admissionGeneration": 3},
    "revision": 4,
    "items": [{"inputId": "opaque-id", "text": "draft"}],
    "restored": []
  }
}
```

`ownerEpoch` is the registry admission generation, not a turn generation.
`queueState.scope` equals `queueBinding` without `version`. A canonical owner
socket serving a permanent alias rewrites only the attachment `sessionId` in
these fields and in `inputStarted.scope`; IDs, revision, owner identity and
text remain unchanged. A known but unprojectable or over-bound owner reports
`queueState: null`; a non-live owner reports both fields null. These are
**unavailable**, never an authoritative empty queue. The legacy text-only
`queue`/`restored` fields remain non-authoritative compatibility data.

Each snapshot allocates a positive monotonic revision per ACP process/session
before asynchronous delivery. A queued follow-up is inserted under its exact
`inputId` with immutable owner-created-at/admission coordinates. Snapshots
include only display-eligible entries for the current owner admission and
retain insertion order; duplicate text is not deduplicated. On that exact
input's user `input_started`, the backend removes only its ID, then emits an
`inputStarted` v1 `{version,scope,revision,inputId,text}` followed by a
higher-revision queue snapshot. Its start is a separate native-model evidence
boundary, not consumption of other queued IDs. A refused exact ID removes
only that queue entry, retaining durable UNKNOWN disposition. Turn-end
restoration moves each remaining display entry to `restored` with the same ID;
this retained read-only row is not an actionable local draft, and is cleared
only by explicit queue clear. Old-admission rows are not inherited by a new
owner, nor deleted from durable disposition evidence by the projection.

Bind only from a trusted new/load result (or owner-ready where actually
forwarded). The currently mounted proxy consumes auto-reconnect ready without
forwarding queue metadata to the client; after an incarnation change, remain
unavailable until explicit trusted new/load. Before binding, buffer at most 32 validated v1 queue updates and input starts
for the receiving attachment. Compare them to the trusted result *before*
showing it: a matching logical session/thread callback with the same
created-at and a higher admission epoch poisons a delayed older trusted result.
That result stays unavailable until another explicit trusted load at or above
the observed floor. A different created-at or same-epoch conflicting identity
is ambiguous and requires a load initiated after the conflict. Once the
binding is trusted, apply only buffered events with its exact scope and a
higher revision, in revision order. A newer/ambiguous owner-scope callback
hides the old binding but cannot itself rebind. Reject lower revisions,
contradictory equal revisions, old-scope starts, and duplicate exact-ID starts.
A malformed or oversized prebind buffer is unavailable until a later explicit
trusted load initiated after the uncertainty; do not infer empty queue or
retry. Back-end snapshot bounds are 32 displayed
entries across `items`+`restored`, 4096 UTF-8 bytes per text, and 65536 total
bytes. Overflow preserves underlying queue/disposition IDs and reports null.

Canonical producer and reducer-order example:
`tests/fixtures/acp_exact_id_queue_v1.json`. It is synthetic provider-free
contract data. Actual Toad mounted reducer/presentation acceptance and
independent exact-byte review remain separate gates.
