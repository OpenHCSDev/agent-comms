# ACP private native cursor v1 (read-only provenance)

This is **not** an input disposition, Pi acceptance, completion, ACK, retry,
response permit, or mediated-write authority. No provider/live activation is
implied. It is distinct from the exact-ID `queueBinding`/`queueState` contract.

On an explicitly configured private N/K owner, `agentComms.privateNativeCursor`
is included in trusted `session/new` and `session/load` results, owner-socket
`ready.agentComms`, and subsequent `session_info_update` metadata. The owner
may emit identity updates before `ready`; these callbacks **must not**
establish a client binding. The proxy currently consumes `ready` internally
and does not forward its private cursor to an already mounted Toad client after
automatic reconnect. Such a client remains unavailable until an explicit
trusted new/load result; a callback or `configOptions` is never a substitute.
The v1 envelope is:

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
allocated before awaiting delivery. A trusted new/load result (or a trusted ready **only where actually delivered
as such**) can bind a new scope, including a lower revision after process
replacement. The mounted Toad client currently has only new/load for this
purpose. Define the logical attachment key as exact `sessionId`, `wireRootId`,
`ownerThread`; the incarnation additionally includes exact `ownerCreatedAt`,
`ownerPid`, and positive integer `ownerEpoch` (a boolean is not an integer).
Do not sort timestamps. Once bound, a same-logical-key authenticated callback
with the same `ownerCreatedAt` and strictly greater `ownerEpoch` **quarantines
and hides** the incumbent, even if the callback says `none`; it does not bind
the new epoch. Different `ownerCreatedAt` or same-epoch conflicting PID on the
same logical key is ambiguous and also quarantines. A null scope for the
receiving private session cannot sustain an incumbent proof and hides it.
Unrelated logical attachments and lower-epoch callbacks are ignored. While
quarantined, ignore *all* callbacks (including old-scope higher revisions)
until an explicit trusted new/load result. After that result, reject older
scopes, lower revisions, and contradictory equal-revision payloads; identical
equal-revision bytes are idempotent. Never rebind from a callback. Malformed,
unsupported, or absent envelopes are unavailable/hidden, not proof of no work.
Observed registry stop/re-admission with no selected input publishes an
unavailable/null-scope or new-epoch `none` update; watcher delivery is not an
instantaneous registry-change guarantee. `none` and `unavailable` carry owner
scope and revision whenever the owner is known. A stopped owner may have a
null scope; this never authorizes a consumer binding.

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
a supported mid-session admission bump: trusted epoch-2 `proven` revision 2,
new epoch-4 `none` callback revision 3, quarantine, delayed old callback,
explicit trusted epoch-4 `none` load, delayed old callback rejected, and
same-epoch unavailable/equal-revision conflict. `autoReconnectReadyForwardedToClient`
is false. The delayed callback is an adversarial sink-order control, not a claim
that one same-process producer allocates revisions out of order. The provider-
free producer regression tests observed stop/heartbeat, new-epoch `none` and
no replay of old-epoch native proof. This fixture is not a live ghost trace. Mounted-client acceptance requires an independent Toad
reducer/presentation test; backend metadata alone cannot prove painting.
