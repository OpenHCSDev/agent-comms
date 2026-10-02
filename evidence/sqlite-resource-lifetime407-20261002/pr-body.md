## Original407 SQLite read-resource closure

The existing CoordinationStore owns bounded read-only snapshot acquisition and cleanup; contention is typed unavailable rather than missing or unsupported data. All native/source/handling/recovery consumers migrate. Gateway identity checks and projection use the same original connection; SchemaMeta owns version checks once.

Production: 10 files, 304 additions / 341 deletions. No schema/native/budget/timeout change.

Installed candidate: 311 files source-equal; 4 real SQLite/canonical private-source controls pass in 1.45s. AST before/after includes all Python production/tests/tools, zero omissions. Full evidence and original limits: evidence/sqlite-resource-lifetime407-20261002/READY-CORE.md.

Paired Toad332 is Arendt-owned. Parent owns fresh configured channel/live acceptance; original407 is preserved, never replayed. The original crash does not identify its originating SQL statement.
