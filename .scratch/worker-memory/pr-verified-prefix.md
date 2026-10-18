## Owner change

`PrivateEvidenceRead` remains the sole owner of how far one native session source is verified. Its prior `size` and SHA-256 have become one named prefix state (`UnverifiedNativeEvidencePrefix` or `VerifiedNativeEvidencePrefix`), not a second index or cache.

`CursorDelivery` now owns exactly one `NativeEvidenceScope` for its ACP session-publication lifetime. `CursorPublication.observe` serializes all use, passes that same scope through `NativeSourceCursor`, and `CommsAgent.shutdown` closes it after session/input producers drain. A repeated cursor observation verifies the retained byte prefix but decodes only appended physical rows.

A replaced inode, truncation, or changed verified-prefix hash raises `NativeEvidencePrefixChanged`. `NativeEvidenceRead` discards every retained entry, reopens the canonical path into `UnverifiedNativeEvidencePrefix`, and re-verifies/decodes from byte zero once. It never continues from the stale offset and malformed evidence still fails closed.

## Consumers

At turn start, `CursorPublication.trusted_metadata` enters `NativeSourceCursor.read`. `SourceCoverage` selects at most two original SQL inputs. `NativeContextProof.read_evidence` needs the header and selected tracked user entry ID; `read_tracked_input_digest` needs that same tracked user's digest. Neither consumes assistant/tool rows, transcript tails, or a row count.

## Verification so far

- `pytest -q tests/test_native_evidence_custody.py --no-cov`: 25 passed
- `pytest -q -n0 tests/test_cursor_load_recovery.py --no-cov`: 1 passed
- Rewritten-source coverage includes in-place truncation/rewrite and inode replacement; both prove the new header/input after byte-zero re-verification.
- `tests/test_native_source_cursor_scale.py` currently has seven unrelated setup failures because it monkeypatches removed `coordinated_runtime._trusted_package`; no changed path ran before those setup errors.

## Acceptance pending

- Real Toad-main builder runtime for this branch.
- Same 173,654,666-byte / 30,841-row native session fork: 20-minute RSS and repeated turn-start timing, compared with merged #793.
