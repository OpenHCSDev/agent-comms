# Display selection owns a captured read boundary

User report: a transient display-scope error in the open IRC view while ten
messages arrived together. Today's Toad logs and native diagnostics contain no
record of that notification. The exact existing error is raised by
`BusPresentation.snapshot` after three whole-file consistency attempts.

Read first: `DisplaySelection`, `BusPresentation`, `LockedStore`,
`RegistryStore`, `Registration`, `ChannelCatalog`, `ReadLedger`,
`DMDisplayBasis`, `HistoryViews`, and Toad's `ChannelConversation`.
`source-before.json` uses the refactor-audit Package/ParsedModule machinery:
311 Core modules and 289 Toad modules, zero parse omissions. Its 67 selected
declaration/call sites are syntactic evidence, not dynamic-resolution proof.

The existing registry owns both stable identity/membership and changing
heartbeat/turn facts. A file replacement is consequently wider than the
question a display predicate asks (IDEN-7). Three retry attempts and a new
counter would preserve that mistake. `LockedStore.reading` already provides
the resource needed to capture a document while it cannot change.

The owner change uses those existing shared document resources during the
short display capture and opened bus boundary. Raw bus iteration remains
outside those resources. Registry, catalog, and read-ledger values are read
once; `DisplaySelection` derives its fields from those same values. The
optimistic revision loop and its terminal failure are deleted. Channel read
acknowledgement uses the same captured scope, preserving actual identity,
projection and painted-membership checks.

Review the related DM page read for the same whole-registry invalidation
before editing: use its existing identity and scope types, not another
provenance store. Preserve genuine rename, reuse, bus replacement, and
noncontiguous-tail refusal.

Implementation is not yet complete or installed. Final batched checks and
the actual installed Toad/IRC path follow the coherent owner change.
