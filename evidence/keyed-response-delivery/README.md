# PR331 automatic response delivery: source and conversion receipt

Production correction: `ce05cc04` (strict tagged receipt core `805ca4ab`).
Base integrated: `6bd8c423`, including PR323/325. Parent owns live cutover.
Dalton owns PR330's authoritative native A→B→A test; no duplicate native test
is added here. Both updated skills and the 22:16 archive's pattern README,
IMPL-14 classification, chain_terms.py, BOUND and TIME-9 were reread.

## Deletion and ownership

353 production lines deleted, 378 added against the integrated base. Deleted
bare `publish_keyed_response`, `_IndexedResponse`, `_parse_response`, old
response-only reader branches and initial-only caller names. No aliases remain
in production, tests, benchmarks or executable evidence helpers. Renamed the
single existing checkpoint source table to `DeliverySources`; `Addressed`
references its declaration. No second bus row, delivery store or codec subclass.
The one-time conversion operator is also deleted from the shipping tree.

One response row owns a declaration-tagged `ResponseDeliveryPolicy` composed of
its immutable `DeliveryManifest` and `KeyedResponseReceipt`. FieldCodec derives
kind from the registered family declarations, rejects unknown/missing fields and
types, and the boundary rejects noncanonical encodings. No union-by-field-shape
selection remains in production. This closes IMPL-4/5, MEMB-1/5, BOUND-1/2/3,
and TIME-1/2/3/9 for this delivery surface.

`ResponseConversation` captures original bus-attested sources from the real
parent execution assignments inside the existing response publication lock and
SQL transaction. Its audience contains the original frozen recipients plus
executable original senders, excludes the responder, and respects the exact
reply route. Later channel membership is not historical authority. Original
publication registers the executable sender outside the response transaction;
response append opens no second SQL connection and registers no participant.

The receipt owner checks root, public envelope digest and the canonical
execution/route publication key. Full parsing refuses duplicate publication keys;
certified parsing uses the same semantic validator and sealed unique-key index.
WakeCandidateIndex consumes that same validated declaration, deleting its raw
receipt mirror. Replies enter both delivery admission and ambient pointers.
Response policy permits bounded consideration/IGNORE, including direct/mentioned
informative replies, avoiding a required ACK loop. Existing wire-lock-through-
append, source fences, UNKNOWN, idempotence and no-resend boundaries remain.

## Executed evidence

All tests used this tree's noneditable installed Python 3.14 wheel. No CI wait.

| Command scope | Result | Receipt |
| --- | --- | --- |
| coordination/source fences and wire callers | 53 passed, one scale case deselected, 13.09s | `final-wire.log` |
| tagged receipt/response/index correction | 38 passed, one scale case deselected, 9.13s | `tagged-second.log` |
| full/certified receipt + checkpoint tests | 40 passed, one scale case deselected, 11.05s | `corrected-readers.log` |
| final candidate projection + receipt/awareness cases | 33 passed, 9.22s | `caller-final.log` |
| authoritative PR330 test on strict tagged implementation | 1 passed, 12.85s | `tagged-native.log` |

Reproduction: `.venv/bin/python -m pytest -o addopts='' -q
 tests/test_keyed_response_delivery.py tests/test_private_bus_checkpoint.py
 -k 'not over_1000'`; final projection shard substitutes
`tests/test_wake_candidate_index.py` for checkpoint tests. No failure is ignored.
The large scale case was run earlier; it is not an additional gate here.

Malformed receipt cases exercise root, digest, canonical key/route, exact types,
unknown field, and duplicate valid publication key. Each semantic case directly
exercises the shared decoder. Real cold full certification rejects corruption;
the existing certified reader refuses changed bytes at its earlier prefix fence
without rewriting its index. This does not claim a forged valid certificate was
accepted then rejected by semantic decoding. Missing/unknown kind is refused
instead of falling back to field-shape guessing. Real DM/channel publication
also proves optional candidate indexing, awareness, one selected assignment,
idempotent polling and missing-native-proof refusal.

Native receipt uses Dalton's test at PR330 `a65cc7b4`, installed immutable Pi
bundle `native-current-5fdef596596173bd`, and a local HTTP model fixture only.
Two real owners launch through normal production entry points. B receives A's
question, publishes a real response, idle A automatically receives it with no
explicit drain/user kick, attached ACP proves its exact native input ID/source,
and IGNORE results in exactly two provider requests and two wire messages.
No paid provider was called. Dalton was sent `ce05cc04` directly for a final
independent post-caller-correction run; record its separate result when received.

Original RED receipts are retained. `native-return.log` initially waited on an
absent ephemeral selected-status field; the authoritative test now checks the
stronger typed VerifiedCursorObservation and input identity. `final-index.log`
contains an additional intermittent timeout after A had already received the
reply, plus two real cold-recovery failures caused by a remaining literal table
name. The literal is fixed (`cold-fixed.log`, later checkpoint passes). The
single cursor timeout's cause was not established; a one-shot cursor projection
race was suspected, not verified. Later authoritative strict-tagged native run
passed. Do not describe this as proof that all intermittent cursor failures are
resolved. Other red logs show corrected fixture migration/expected-error and
conversion-seal issues, not silently discarded results.

## One-time durable history conversion

The tested operator is preserved only in Git history:

```sh
git show 805ca4ab:tools/convert_response_delivery_once.py > "$operator_path"
"$new_runtime_python" "$operator_path" "$stopped_wire_root" \
  --old-python "$previous_runtime_python"
```

Use an owned persistent operator path. Stop writers first; parent controls this
operation. The predecessor interpreter verifies its old schema and holds the
real wire/bus locks throughout conversion. The new interpreter creates a backup
of bus/index/metadata, rewrites in place, certifies a staged derived index,
refreshes inode-bound seals after rename, publishes the marker last and fsyncs.
It refuses a pre-existing backup/repeated conversion. An interrupted cutover
fails closed and requires explicit operator recovery; runtime has no old/new
reader or implicit repair. Remove the extracted operator after use.

`live-copy-tagged-final.log` records the successful copied-live rehearsal:

- 162 public message projections compared exactly equal before/after (body,
  message ID, sequence, timestamp, route and remaining public fields).
- Current responses 153,156,159,160,162 gained frozen delivery manifests using
  their real retained parent executions; no current-membership reconstruction.
- 37 response runtime receipts below the already established admission floor
  149 had no retained parent execution. Their public history remains in place,
  their original private bytes remain in the pre-conversion backup, and they
  gain no invented audience or permission to replay. Floor stays 149.
- Original root identity is retained. No execution/UNKNOWN barrier is reset and
  no native input is submitted. Executable original sender participant entries
  are registered from the historical frozen identity authority.

The copy was made under verified predecessor locks from the routed live root;
SQLite was copied using its read-only backup API. Conversion mutated only this
worker's persistent copy. The live database, route and owner processes were not
modified. Earlier red conversion receipts include unknown retired execution,
new-reader/old-index schema rejection, and staged-inode rename seal mismatch;
the final operator explicitly addresses each.

## Architecture checks and remaining handoff

`architecture.json`: no positive existing class-size/other packaged-ratchet
measure. Census has -32 long-chain terms (no touched file increase), -13 exact
type checks and -46 string subscripts. Final NRA scan includes full package
context and five reported source paths. The duplicate receipt raw mirror found
by the previous scan is deleted. Two remaining findings concern pre-existing
untrusted `CommittedAppendHint` boundary checks outside the edited parser.
This CLI did not report complete/omitted detector counts; no zero-omission
claim is made. Ruff and diff whitespace checks passed for changed owners/tests.

No diagnosed source/caller blocker remains in this response correction. Parent
must review, run the one-time conversion on the stopped live root and activate
the installed candidate. Independent final native PR330 result is tracked with
Dalton. S15's authoritative plan file was not found; optional advice did not
block this fix, and this receipt does not claim implementation of unseen S15.
