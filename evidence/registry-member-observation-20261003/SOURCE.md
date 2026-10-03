# Acquired registry member reads

Base: merged599 `3e5c97f9`. Existing checkout/branch reused; draft605.

`RegistryNames.require` now owns namespace membership beside canonical-name
resolution. Its type parameter describes the returned declaration; it adds no
stored/dataclass field. Historical RegistryProvenance continues to own only
recorded threads/aliases. It has no live status or process authority.

The original Snapshot status/active-member behavior lives on data-free
RegistryPresence and is composed into RegistryDocument and RegistrySnapshot.
The latter remains RegistryProvenance too; its concrete dataclass annotations,
field order and strict JSON encoding are unchanged. Active refusal remains
RelationViolationError; ordinary absent member/status remains UnregisteredThreadError.
Canonical-name resolution alone still accepts an unknown/unregistered name.

Registration.require/status now consume the determining acquired document. Their
immutable Thread/ThreadStatus result escapes acquisition without copying six maps.
Goal history uses the same namespace membership owner. Last-seen resolves the
original member through that owner and reads its required timestamp: no fabricated
zero. Explicit recorded zero remains valid. Both restoration and ThreadView derive
that same timestamp. Current-format RegistryDocument.from_wire already rejects
missing member timestamps before these reads.

Detached multi-fact snapshots are retained: captured native owner/generation,
turn/admission/compaction fences, source attachment, roster-wide projections and
external read cuts. This batch neither changes their custody nor removes their
locks. Collection-producing reads still copy the returned collection. Allocation
/name-reservation predicates are distinct from requiring an existing member.

Source-before/after uses the existing NRA parse_python_module_roots on all311 Core
production modules, with explicit omission reporting. Attribute names are source
leads, not proof of dynamic receiver resolution. Semantically read determining
namespace/document/store, Thread/ThreadStatus/ThreadProvenance, Registration,
ThreadView, historical/read/custody consumers. Public Registration signatures and
frontend call sites are unchanged. Original snapshot-dependent fence consumers
remain on their original cut. No provider/worker/root/schema/native mutation.

Patterns: IMPL-12 (shared member/presence behavior), BOUND-2 (read through the
existing acquired owner), IDEN-7 (whole-registry projection wider than one result).
No native/whole-turn latency gain is asserted.

Preliminary source probes retained: record_schema(dict/Mapping) and decoding a
RegistrySnapshot Mapping are unsupported by the original FieldCodec API; this
batch does not introduce a codec/compatibility reader to manufacture that control.
Document strict codec roundtrip and actual dataclass field/type inspections work.
Project-config mypy stops at existing newer-syntax tracked_turn.py; explicit3.14
boundary parse reports existing skipped-dependency Any/Mapping annotation issues,
so neither output is claimed as a whole-project type-check pass.

Installed CLI/member qualification completed in the explicitly granted thin540 holder.
See READY.md and installed-receipt.json; no accepted599 provider journey repeated.
