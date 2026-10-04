# Saved native startup owner

Scope starts at merged562 `cc56dbee` plus its configured acceptance5e494e04.
The existing checkout is reused; no environment or native artifact is created.

The read-only reopen helper constructs `DiskEntryStore` solely to locate header
identity. Its constructor rebuilds the complete JSONL selector index. The actual
native `SessionManager._setSessionFile` constructs that store again, before
startup services, source writes, capability attestation or prompt delivery.
This is duplicated full-history work, not two required semantic authorities.

Use `NativeSessionIdentity` for the read-only header/path boundary and the
existing native `EntryStore.validateHeader` declaration for header validation.
`SessionManager` remains the full-history loading owner. Canonical regular,
single-link, nonempty file and before/after revision fences remain. Custody's
expected identity and actual get_state attestation remain before input. Migrate
all existing identity-read callers and remove the free-standing reopen reader;
no replacement registry, cache, source flag, fallback or parser.

Patterns: BOUND-1, BOUND-2, IMPL-4. AST will use the existing NRA Package loader
and native Acorn parser; those references are source evidence, not dynamic proof.
Final checks cover malformed history rejected by its actual native owner without
repair, expected-identity refusal, and installed saved-source startup/retirement
without provider input. The preserved147.617s configured562 journey is not
repeated and gives no controlled improvement claim for13.562s/98.141s.
