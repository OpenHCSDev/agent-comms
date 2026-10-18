## Owner change

`PrivateEvidenceRead` remains the sole owner of how far one native session source is verified. Its prior `size` and SHA-256 are one named prefix state (`UnverifiedNativeEvidencePrefix` or `VerifiedNativeEvidencePrefix`), not a second index or cache.

`CursorDelivery` owns exactly one `NativeEvidenceScope` for its ACP session-publication lifetime. `CursorPublication.observe` serializes use, passes that same scope through `NativeSourceCursor`, and `CommsAgent.shutdown` closes it after session/input producers drain. A repeated cursor observation verifies the retained byte prefix but decodes only appended physical rows.

A replaced inode, truncation, or changed verified-prefix hash raises `NativeEvidencePrefixChanged`. `NativeEvidenceRead` discards every retained entry, reopens the canonical path into `UnverifiedNativeEvidencePrefix`, and re-verifies/decodes from byte zero once. It never continues from the stale offset and malformed evidence still fails closed.

## Cold-start finding and removed work

The 173,654,666-byte saved session has 30,841 physical JSONL rows and 2,167 retained input entries. In live use it had delayed first turns by roughly 20 seconds to more than three minutes because two independent turn-start consumers could decode the complete session concurrently.

The second decoder was `FreshPrivateSession.require_launch_header()`: it consumed row one, then exhausted `_read_private_file()` only to complete revision validation. It now calls `PrivateEvidenceRead.header()`, which hashes and verifies the complete captured prefix but JSON-decodes only row one. A 201-row regression fixture proves one decode while retaining whole-file mutation checks.

The launch validator and cursor publisher have separate concurrent lifetimes, so they do not share one mutable descriptor/reader. They now share the same `PrivateEvidenceRead` verification semantics; the longer-lived cursor path additionally reuses its one `CursorDelivery`-owned scope.

## Consumers

At turn start, `CursorPublication.trusted_metadata` enters `NativeSourceCursor.read`. `SourceCoverage` selects at most two original SQL inputs. `NativeContextProof.read_evidence` needs the header and selected tracked user entry ID; `read_tracked_input_digest` needs that same tracked user's digest. Neither consumes assistant/tool rows, transcript tails, or a row count.

## Measured comparison

Same source session, native package `84d389a90e2228d6`, Toad `8afa9b641`, isolated native fork, and `taskset -c 4-9 nice -n 10`:

- Baseline Core `47bc74607`: cold turn-start read **128,519.937 ms**; 20-minute RSS max/final **160,972,800 / 160,972,800 B**, USS max **141,119,488 B** (41 samples over 1,204.036 s, return 0, source hash unchanged).
- Candidate Core `0ab7f8952`: cold turn-start read **1,372.306 ms**; warm median **139.497 ms** over the six observations in the 30-second cold-start run; RSS max/final **146,489,344 / 146,489,344 B**, USS max **126,599,168 B** (31 one-second samples, return 0, source hash unchanged).

The candidate includes current main's #809 buffered fresh decoder as well as this PR's header-only decode, so the 47bc-to-candidate timing is the complete current branch result rather than an attribution to either change alone.

## Built and verified

- Built runtime: `/home/ts/.local/share/agent-comms/runtimes/toad-8afa9b641-core-0ab7f8952-native-84d389a90e2228d6-20261014T025929Z`
- Builder completed route/import/version checks and opened saved thread `codex-shepherd`.
- Focused combined batch after merging Core `e27e2182b`: `67 passed, 1 skipped in 1.84s`:
  - `tests/test_native_evidence_custody.py`
  - `tests/test_native_evidence_append.py`
  - `tests/test_cursor_load_recovery.py`
- Refactor ratchet passed at branch head `1873f450f` with no increase.
- Rewritten-source coverage includes in-place truncation/rewrite and inode replacement; both prove the new header/input after byte-zero re-verification.
- No serving worker was modified or injected; measurements started isolated workers on fresh native forks, and original source hashes remained unchanged.

## Remaining acceptance

- Complete the uninterrupted 20-minute candidate RSS series matching the completed baseline duration.
