# Session identity publication

Base: merged #616 `966e761d7932b4b3a2a54852789dccbf372d6097`.
Mendel owns `SessionLifecycle.sync_identity` and its publication consumers.
Reuse the existing checkout; no holder loan or native/build/inspection claim.

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
