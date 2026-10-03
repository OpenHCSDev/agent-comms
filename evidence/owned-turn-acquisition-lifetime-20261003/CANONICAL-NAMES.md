# Recorded name resolution and requested-owner admission

Source correction following reviewed92708dcb, not a Ready receipt.
Pattern IDEN-7: the loaded-resource query had widened alias resolution into
membership validation for every unrelated loaded binding.

RegistryNames carries the original alias-resolution method through MRO for the
mutable RegistryDocument and frozen RegistryProvenance/RegistrySnapshot.
It has no instance storage or dataclass fields. Their existing declarations,
immutability and FieldCodec field layouts remain unchanged. The mutable and
frozen dataclasses cannot directly inherit each other, so this shared behavior
base removes the need for per-query map projections.
Require/status/active/process/lease consumers keep
their separate membership and custody decisions. SessionLifecycle resolves each
loaded name through the original captured snapshot; RuntimeRequest.require_owner
still validates the actual requested thread and process before that lookup.
A removed unrelated binding therefore cannot prevent an otherwise valid request.

65 existing registry resolution expressions migrate together: registry snapshot
and mutable document operations, Registration, admission/lease/input owners,
goal/wait/relationship decisions, live and archived history, publication,
read ledger/display basis and recorded incarnation consumers. Mutable document
and Registration queries invoke that inherited method directly on the acquired
document: no snapshot, copying, second read, retained namespace or cache.
Archived consumers continue
to receive their original certified RegistryProvenance, not today's registry.

The remaining alias lookups have different contracts: RegistryDocument.rename
tests whether a proposed name is already owned (one-argument lookup);
DeliveryScope resolves its already captured delivery selector; ResponsePolicy
consumes its supplied mention-policy aliases. They are not registry loaded-owner
membership queries. No wire schema, alias data or persisted state changes.

NRA parser before/after covers all311 current production modules, zero omissions.
The saved inventories name declarations and enclosing consumers. Receiver/MRO
resolution was read from source; AST syntax is not a dynamic execution proof.

The existing runtime socket control now retains an actually bound removed owner
before the valid loaded owner, then exercises the same alias rename. It also
asks the original RuntimeRequest owner validator to refuse the removed target.
Installed controls05 ran this original socket journey against c4ce7f34 and
passed, including removed unrelated binding, valid renamed alias and refused
removed requested target. It does not qualify the later no-copy source refinement.
The earlier0d configured receipt does not qualify this batch.

Original controls04 was interrupted after582.74s with no completed tests;
both private stack-inspection attempts were denied by process access policy.
Its original negative log is retained. Only its exact owned provider-free
driver1832816/birth53441930 received SIGINT, through Platform.send; it exited2.
No public process, native attempt or group was signaled. Bounded controls05 then
confirmed the namespace regression and stopped during the pre-native failure
control's owner.shutdown. Its full original stack shows waiting on async cleanup
while the executor workers are idle; it does not establish a physical store-lock
cause. The original negative is retained. Final changed checks and the distinct
configured-fork journey remain pending.
