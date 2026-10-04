# Session identity publication

Base: merged #616 `966e761d7932b4b3a2a54852789dccbf372d6097`.
Mendel owns `SessionLifecycle.sync_identity` and its publication consumers.
Reused the existing checkout and the explicitly granted thin540 package holder.
Native086 was read/executed under its new #619 lease; no native/build/inspection edits.

The same identity synchronization builds and sends complete metadata twice when
both name/title and worktree change. Each copy reads configuration, goal,
runtime information, queue and cursor, and each publication invalidates clients.
The existing session owner should publish those related fields together once,
then complete the same synchronization attempt for all three existing publication
coordinates. Actual transport acceptance remains `RuntimeServer`'s result; these
coordinates do not manufacture an acknowledgement when no client is attached.
Keep `Thread` and `RegistrySnapshot` as the identity/status owners. Preserve the
original method's canonical-name return contract and process/status-driven idle
child retirement; no new result type, cache or state flag.

AST before: existing NRA/refactor-audit `Package.load` across src/tests/tools,
311/364/53 modules, zero omissions. The sole method and inherited implementation,
all seven production callers plus three fixture callers and all publication
coordinate reads/writes are recorded in `before.json`. Method references are
lexical leads; the callers were read semantically. No dependency API changes.

The following additional registry reads are deliberately not conflated with
identity announcement: private N/K performs later scheduling admission; manual
compaction captures after taking its turn lock; project retirement observes
current configuration; settings apply and prompt ingress reach their actual
target boundaries. Their freshness is a different fact. This batch does not
claim that copying a detached registry snapshot can be globally removed, nor
that the original 13/98-second spans are explained by this source duplication.

Catalog IMPL-12: two copies of metadata/publication in one owner become one.
Existing title/worktree maps are client delivery coordinates; registry remains
canonical and they grant no turn, input or native custody. Do not create a
second identity or observation store. Final checks cover simultaneous changes,
idempotent sync, raised publication retaining synchronization coordinates and actual
saved-native/registry/client update with no provider input. Tests come after
the source batch; an installed holder requires its own fresh named grant.

## Installed qualification and release

Production: `3759e2038df431595ec5388273415ca643619d8d`; final tested
source `8be44b86b77b003831b9c1ce5a43a35f8a67d5bd` has identical src/stack.
One production file: 1 line added, 8 deleted. Existing callers retain the same
canonical-name return and fresh admission/retirement reads. Before/after AST
roots have no parse omissions; dynamic dispatch remains a stated lexical limit.

The existing thin540 interpreter loaded the normal wheel: all 342 installed
members matched, all 311 Python source members and three declared force-includes
matched, SDK 0.12.1 and all nine dependencies remained unchanged; pip check passed.
`installed01.log`: one changed installed test passed in 5.09s, terminal exit 0.
The real saved-SDK owner prepared its native child and subscribed through the
original RuntimeProxy socket. Combined canonical rename/project produced one
complete update, identical typed metadata reached the subscriber, a raised
transport error preserved the prior synchronization coordinates, and unchanged
sync emitted nothing further. The saved native source bytes were unchanged.
No prompt, provider request, native input or public write occurred.

The actual proxy and attached agent shut down, owner shutdown joined its child;
the final assertions proved all original child identities absent and owned
groups empty. This is installed saved-owner/socket metadata acceptance, not a
physical UI, paid-provider or end-to-end latency measurement. It does not claim
the historical 13/98-second gaps are fixed.

After this terminal result Mendel releases the complete thin540 package/execution
loan and native086 READ/execution lease. Current package is the #619 wheel, not
its archived #616 preimage. Preserve all private roots, source/auth/sessions,
input proofs, UNKNOWN and original basetemps; a subsequent package purpose needs
its own current-package archive and borrower checks. No further borrow is assumed.
