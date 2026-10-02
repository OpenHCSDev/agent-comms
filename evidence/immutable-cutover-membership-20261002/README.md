# Original membership survives carry partitions

After actual #517 goal carry, PreserveRuntimeInstallation returned the same mutable set. The publisher removed goal paths through that alias, corrupting the full original membership used for its later comparison. Target goal carry had already committed; the outer phase had not advanced.

PublishRetainedSummary.protected_files now returns frozenset. RuntimeInstallation owns immutable input/output membership across inherited Reset/Preserve and CarryNative; CarryNative derives its difference through the base behavior. Retain consumers declare immutable membership. The publisher derives its byte-preserved partition and hashes once, before any carry. Replaced mutable alias and duplicate partition are deleted; no new class/catalog/mirror or recovery path.

The single focused check uses installed9ccc package and tracked tools. Original receipt membership remains 39; byte-preserved partition is 38 without changing the full set. It decodes actual RuntimeInstallation and CarriedNativeStore through installed FieldCodec. Actual public target is opened SQLite mode=ro/query_only transaction, target declared DDL and candidate SHA qualified, 492 fact rows/generated cells and rowids match the completed installed receipt. No original-DDL authentication, carry execution, inputs/provider calls/public mutation. This is not a stopped-custody grant or completed publication.

The goal installed receipt owns committed transformation despite outer phase stopped/audited. Parent alone continues from that reviewed installed target and current custody; do not rerun old carry. Old failed receipts/preimages are untouched.

Existing audit Package/ParsedModule before/after mapping parses all 50 cutover Python modules, zero omissions. Caller spelling does not prove dynamic resolution; declarations and each related producer/consumer were read. Existing immutable value/resource lifetime is the ownership pattern; no local goal-only special case.
