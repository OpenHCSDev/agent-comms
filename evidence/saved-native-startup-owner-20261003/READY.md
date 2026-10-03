# Saved native startup — Ready

Production checkpoint `fdd4927f276c71dde2d968ee224261165d20fc12`:
56 lines deleted /62 added across the existing identity owner, selected-session
consumer and its replaced packaged helper. The old free-standing
`validate_native_reopen`, `ReopenSessionHelper` and `reopen_session.mjs` are gone
from production and tests. No compatibility alias or alternate reader remains.
All related retained-summary/capacity callers use `NativeSessionIdentity.read`;
obsolete fake reopen hooks/tests are deleted. Production and tests together:
199 deleted /101 added. The reused installed-owner driver gained configuration
parameters and an actual context RPC; it does not dispatch a prompt.

## What changed

Previously every `SelectedSession.for_launch` used a read-only helper that
constructed `DiskEntryStore`, rebuilt the entire history's disposable SQLite
index, returned only its header identity, then destroyed the index. The actual
native child built it again in `SessionManager._setSessionFile`. Managed ACP
launch, explicit preparation and coordinated selected-owner startup all reach
this existing selected-session boundary.

`NativeSessionIdentity` now owns the bounded read-only identity operation. Its
helper consumes the first line through Node's stream/readline resources, decodes
UTF-8 strictly, and delegates header meaning to existing native
`EntryStore.validateHeader`. It constructs no store or index. Stream resources
close on success and failure. Canonical regular, single-link, nonempty,
correct-owner file checks, before/after `FileRevision`, native package trust and
typed helper decoding remain. Stream read-ahead is permitted; this is not a
claim of physically reading exactly one line's byte count.

Identity selection is not history readiness. Native `main.js` selects the saved
`SessionManager` before startup services. Its `_setSessionFile` constructs the
strict `DiskEntryStore` before making history available, and `_assertLoadedRevision`
retains the file fence. The actual `get_state` attestation still compares the
selected identity; `ReopenNative.expected` still compares the original custody
identity before spawn. Input grants remain after attestation. A malformed tail
or ancestry therefore fails at its full-history owner, without repair or input.
The removed tests fabricated this policy through a monkeypatched helper on
NativeCustody that no longer existed. Actual native-loader controls replace them.

## Installed qualification

Reused `.artifacts/runtime-scoped-input534`, normal wheel and declared `[acp]`
installation; dependency check15 packages. All339 source/resource files and
342 wheel members match installed bytes. The deleted helper is absent from the
installation. Native044 and its full-trust manifest are unchanged; no new native
bundle, environment, checkout, public route, schema or carry operation.

Six installed controls passed in9.77s: identity/read preservation and stripped
preload; malformed header, missing source and symlink refusal; malformed tail
and ancestry rejected by the actual pinned native loader without repairing bytes.
The first invocation stopped at pytest configuration because the small holder
lacks repository-wide parallel/coverage plugins; no controls ran then. That
original log remains. The corrected serial invocation selected only this family.

The existing installed saved-owner driver then opened the real42,116,826-byte
SDK fork retained from configured562, using its actual Sol/HIGH/worktree settings.
Unopened inspection refused, explicit preparation acquired an attested native
child, and the real context runtime RPC completed on that child. The native file
and `.input-proof` hashes remained identical. Normal teardown closed its runtime,
native child and both exact recorded process groups. InputDocument contains no
new inputs. No provider request, input/replay or physical UI was performed.
The raw driver receipt's inherited309 UI scope text is historical; the separate
`installed-custody.json` states the actual backend scope. The source producer is
corrected for future runs; raw receipts were not rewritten or rerun.

## Source evidence and limits

Before/after NRA Package AST covers726 Python production/test/tool modules,
zero parse omissions. Node's bundled Acorn/Acorn-walk covers274 compiled native
JS/MJS modules and both helper versions, zero parse omissions. Native TypeScript
declarations and transitive dependency implementations are outside that declared
runtime query; lexical aliases/dynamic resolution are not proven by these counts.
The complete identity/custody/attestation/loader call family was read semantically.

No true source or installation blocker remains. This removes a complete extra
full-history index construction before startup; it does not claim that original
13.562s finish-to-next-request or98.141s summary duration is fixed. The configured
562 run is preserved and was not repeated. Commit/fork helper indexes have
separate real writer/source custody; they are not silently treated as this
discarded identity-only index. Original public history, UNKNOWN and original560
ACP_UNCONFIRMED remain untouched.

Parent owns merge/publication. Sch owns normal receiving; no native build or
runtime reset is needed. Private root `/home/ts/.cache/agent-scratch/m567ctx`,
controls `/home/ts/.cache/agent-scratch/m567-control`, existing534 holder and
original m562 source remain protected until receiving/evidence release.
