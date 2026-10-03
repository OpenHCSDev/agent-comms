# Registry member observation without whole-document projections

Receiving source:merged5993e5c97f9; original599 qualification stays frozen.
Integration owner:Mendel. Existing checkout reused; no package/env/native changes.

Concrete source:Registration.require/status each creates a complete RegistrySnapshot
for one member result. That copies threads/statuses/last_seen/aliases and both generation
maps after reading the determining document. Snapshot copies remain legitimate when
an immutable multi-fact cut escapes acquisition; a single immutable Thread/ThreadStatus
read under RegistryStore.reading needs no such projection.

Reuse the existing registry namespace/status behavior through shared capabilities
for the live document and detached snapshot, keeping historical RegistryProvenance
free of current status/process authority. Migrate original require/status/name readers
and related decision consumers as one source batch; preserve aliases, removal refusal,
actual target validation and every lease/admission fence. No state/status mirror,
new cache, string fallback, native/package/schema change or latency improvement claim.

603 inspection/native source query methods remain Arendt/parent-owned. Parent601
FieldCodec remains disjoint. Existing core publication/retirement and root/source/UNKNOWN
proofs are untouched. Source/AST reading first, one changed installed boundary check
last on a released existing holder; no repeat configured599 provider or new environment.
