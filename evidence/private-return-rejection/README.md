# Private return prompt rejection

Actual nra-architecture private session copied read-only (78,483-byte source at
capture), with its native input proof and original modes. A new synthetic input
ID/prompt on the copy reproduced `No API key found for openai-codex` from the
pinned 5fde native RPC command. A fetch fence recorded zero network attempts.
Neither the copied history nor the live source changed. Message 152 was never
replayed. Full diagnostic receipt is adjacent; no prompt or credential is included.

Cause: `prepare_native_pi_rpc_launch` overwrote the explicit canonical
AGENT_COMMS_NATIVE_CONFIG_DIR with PI_CODING_AGENT_DIR. A restarted worker's
latter directory is the credential-free private session settings directory.
Preserve the explicit canonical path, consistent with the other native launchers.

Correlated prompt refusals now carry their existing typed Response through
NativePiPromptRejected into the private diagnostic. Channel notices point to it;
raw response data and error prose are not copied into the channel. UNKNOWN input
handling and no-replay behavior are unchanged. Duplicate/mismatched ACKs remain
protocol failures and cannot be accepted as prompt success.

Actual pinned CLI/loopback coverage: missing credentials preserve the native
rejection without a provider request or input commitment; canonical custom model
and key remain usable across two fresh private children reopening the same saved
history, with exactly one distinct committed input per request. Existing stop,
length, 429 and configured-provider checks also pass. Fixture contextWindow now
matches the current native compaction reserve contract (272000); the old 8192
fixture could not start with reserve16384 and failed before assertions.

Queue predecessor acceptance: 26 actual installed owner cases passed in231.49s;
28 reservation/journal cases passed in3.33s. Includes revoked future queue refusal
before summary/provider work, no original admission, unchanged native history.
Parent owns integration/deployment; native package unchanged.

## Final installed acceptance

- Noneditable wheel: six actual native cases passed in19.93s, including the fresh
  child reopen and native refusal diagnostic cases.
- Private alert/no-replay projection and structural diagnostic privacy passed.
- An existing headless fixture still fails `reserved == unknown`; reproduced on
  the predecessor installed wheel as well. It supplies a mocked preflight Done
  without binding input. This is not a new failure from the rejection patch.
- The exact live private history copy also completed one NEW synthetic input using
  a loopback-only provider/catalog (272000 context), returning LOCAL_COPY_OK. One
  HTTP request, one new native input, prior history retained as a byte prefix,
  original live history unchanged. This validates the saved-history path without
  spending provider credits; it is not a live OAuth/provider acceptance claim.
