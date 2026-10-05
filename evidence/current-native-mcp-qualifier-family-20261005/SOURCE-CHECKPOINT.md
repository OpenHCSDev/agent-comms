# Original native MCP qualifier: source migration checkpoint

Base actual Core main38a0533e5efa12ae0be9266e6b842ac07e3769ab. Paired Toad source draft472 at8c84f6d40e4c0ad4515f45e83c38c9f3987a6092. No package, native, provider, input, test collection, import or test execution authorized for this source repair.

The native producer already publishes original TurnTranscriptUpdate(state=TurnState) through RuntimeServer as TurnChangedUpdate. The qualifier still imports removed TurnStartedUpdate/TurnSettledUpdate and uses those decisions in both Attachment.session_update and the final passive Audit records. Those consumers must retain the original active cut and accept only its owner-matching idle cut, including phase publications and the initial idle replay.

Preserve actual MCP receipt, single echo, two bounded localhost model requests, mounted permission, offered answer, no-controller denial, midturn ledger revocation, disconnect and actual cleanup assertions. No compatibility aliases, fabricated terminal/state or production change.

Source findings also include stale backend.os environment access, an external module loader that omits its selected sibling helper directory, and setup/attached-controller cleanup outside the original retirement scope. The existing environment, SDK, RuntimeProxy, declaration and joined retirement owners will carry these corrected lifetimes. The native and UI qualification remains UNRUN until separately scoped.
