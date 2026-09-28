# Tracked native RPC framing closure (audit281 item2)

Owner: this sidecar. Dalton owns NativeContextProof/proof-journal decoding; those
sections are untouched. Parent owns integration and deployment. Native5fde unchanged.

The tracked reader now consumes canonical PiRpcChannel.receive(strict=True).
Deleted its 1MiB record constant, transport-limit override, separate complete-line
length/newline checks and duplicated decode step. The canonical channel keeps
fragment ownership across cancellation, validates UTF8/JSON/duplicates and
requires newline framing. One current record is decoded at a time. Its memory
still scales with that record; this is not a constant-memory JSON parser claim.
No replacement record ceiling, temporary spool files or history truncation.

AttachedChild owns discarded stderr drainage in io.DEFAULT_BUFFER_SIZE chunks
through EOF, never retaining total output. The former one-shot stderr read could
stop draining a noisy child. A real8MiB stderr writer completes with the drain.

Actual large native cancellation exposed a second bug: asyncio process.wait can
remain blocked after process death if a cancelled reader leaves its pipe paused.
ChildProcess's sole identity-bound group-retirement algorithm now calls the IO
release hook before wait. AttachedChild closes its owned asyncio subprocess
transport there, after the process group has retired. The sole stdlib-private
transport access lives in that implementation owner. Detached children have no
asyncio pipe transport. No independent signalling or retry mechanism was added.

Actual pinned Pi/loopback acceptance: source3passed9.58s. A2,098,094-byte RPC
record and2,097,409-byte complete response pass with native input/context proof.
The predecessor installed core rejects the same real event at the old limit;
failed receipt retained. Additional actual-native cancellation and forced EOF
mid-large-frame retire children, retain one input/context commitment and settle
UNKNOWN without retry authority. One localHTTP request per case, zero paid calls.

Final acceptance:66 shared-child/reader cases passed in24.22s. The noneditable
wheel passed9 actual pinned-native cases in29.27s, including large complete
record/cancel/EOF, ordinary configured-provider success, restart/reopen, missing
credentials, provider429 and length refusal. Installed import was verified under
.artifacts/rpc-framing-installed. No live deployment by this sidecar; parent owns
integration. CI is deferred per owner. Owned generated native session copies
were removed after process retirement; branch, receipts and wheel are retained.
