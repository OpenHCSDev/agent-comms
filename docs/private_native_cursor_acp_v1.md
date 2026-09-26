# ACP private native cursor v1 (read-only provenance)

This is **not** an input disposition, Pi acceptance, completion, ACK, retry,
response permit, or mediated-write authority. No provider/live activation is
implied. It is distinct from the exact-ID `queueBinding`/`queueState` contract.

On an explicitly configured private N/K owner, `agentComms.privateNativeCursor`
is included in trusted `session/new` and `session/load` results, owner-socket
`ready.agentComms`, and subsequent `session_info_update` metadata. The owner
may also emit identity updates before `ready`; these callbacks **must not**
establish a client binding. The v1 envelope is:

```json
{
  "version": 1,
  "scope": {
    "sessionId": "beta", "wireRootId": "<32 lower-case hex>",
    "ownerThread": "beta", "ownerCreatedAt": 1000.0,
    "ownerPid": 1234, "ownerEpoch": 3
  },
  "revision": 2,
  "status": "none"
}
```

`scope.sessionId` is the receiving ACP attachment's session ID (including a
permanent alias); an authenticated owner-socket proxy rewrites only that field
from the canonical owner session before forwarding ready/callback metadata.
`scope` is identical for all updates of one owner incarnation; `ownerEpoch`
is the registry admission generation, not a native receipt. The revision is a
positive monotonically increasing **per ACP process/session** projection order,
allocated before awaiting delivery. A new trusted load/ready can bind a new
scope (including a lower revision if the ACP process changed); only that trusted
result resets the binding. Once bound, reject different scopes, lower revisions,
and contradictory equal-revision payloads. Equal-revision identical bytes are
idempotent. Never bind or switch incarnation from a callback alone. Malformed,
unsupported, or absent envelopes are unavailable/hidden, not proof of no work.
A null `scope` occurs only when the owner itself cannot be established; it
cannot bind a consumer. `none` and `unavailable` still carry scope and revision
whenever the owner is known, so an older delayed `proven` update cannot
supersede a newer reconnect snapshot.

Statuses: `proven` means a selected source has a current-owner native-source
cursor; `coverage_only` means source coverage exists but **no injected input**;
`none` means no current-owner cursor; `unavailable` means this projection
cannot currently establish the cursor. `proven` and `coverage_only` additionally
carry the existing snake_case cursor proof fields (including `covered_seq`,
`injected_seq`, and `input_id`); the row is read-only and must not show private
IDs, paths, or contents. A consumer may display only a separate session
provenance label and an explicit non-consumption tooltip.

The provider-free event-order fixture is
`tests/fixtures/private_native_cursor_v1.json`. It demonstrates trusted
reconnect, a delayed old-epoch `proven`, same-epoch `unavailable`, equal-revision
contradiction, foreign epoch, and a new trusted binding. This fixture is not a
live ghost trace. Mounted-client acceptance requires an independent Toad
reducer/presentation test; backend metadata alone cannot prove painting.
