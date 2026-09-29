# Packaged polymorphism screening ratchet

Parent owns PR409. The updated refactor-audit skill requires literal/type dispatch subject and arm measures. The existing packaged ratchet previously omitted these four screens. It now uses declaration-derived measure membership, shared AST collectors and inherited per-file alignment; no alternate script, hand-maintained measure roster or exception mechanism was added.

The installed noneditable `agent-comms-ratchet` was exercised through real Git repositories at both Core and Toad source roots: 57 focused checks passed. Added behavior covers literal equality/membership/match, isinstance/type/match, growth inside a candidate, reduction, separate subjects/functions/nested scopes, repeated cases, and cross-file growth that another file's reduction must not conceal. The scoped run overrides the repository's broad parallel/coverage defaults; it does not claim a full suite result.

The same installed command passed its actual production change against current main with no measure growth. On real project history f95a47de^ -> f95a47de, it rejected the added restart-queue candidates and measured ArchivedThreadStatus's literal arms growing from three to four while its subject count remained one. This verifies the arm measure catches growth missed by subject count.

These are screening measures: external AST/protocol taxonomies still require ownership review. Three-arm thresholds miss smaller dispatches; per-file aggregate improvements can offset changes within the same file. Existing site-specific ownership/sealing guards remain necessary. No claim of complete S14/T9 closure follows from this tool change.

Production: 125 lines added, zero deleted. The addition supplies missing measures through the existing mechanism; no production mechanism is replaced. Tests: 51 lines added, zero deleted. No runtime store, native package, session/proof or provider path changed.
