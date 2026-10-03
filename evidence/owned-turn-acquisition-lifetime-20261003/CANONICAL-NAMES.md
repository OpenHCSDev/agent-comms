# Recorded name resolution and requested-owner admission

Source correction following reviewed92708dcb, not a Ready receipt.
Pattern IDEN-7: the loaded-resource query had widened alias resolution into
membership validation for every unrelated loaded binding.

RegistryProvenance.canonical_name owns alias resolution without membership.
RegistrySnapshot inherits it; require/status/active/process/lease consumers keep
their separate membership and custody decisions. SessionLifecycle resolves each
loaded name through the original captured snapshot; RuntimeRequest.require_owner
still validates the actual requested thread and process before that lookup.
A removed unrelated binding therefore cannot prevent an otherwise valid request.

65 existing registry resolution expressions migrate together: registry snapshot
and mutable document operations, Registration, admission/lease/input owners,
goal/wait/relationship decisions, live and archived history, publication,
read ledger/display basis and recorded incarnation consumers. Mutable document
and Registration queries derive a snapshot from the already acquired document,
without reopening its lock. Those temporary projections copy the current maps;
they are not another retained namespace or a cache. Archived consumers continue
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
It has not yet run against this changed source. The earlier0d configured receipt
does not qualify this batch.

Original controls04 was interrupted after582.74s with no completed tests;
both private stack-inspection attempts were denied by process access policy.
Its original negative log is retained. Only its exact owned provider-free
driver1832816/birth53441930 received SIGINT, through Platform.send; it exited2.
No public process, native attempt or group was signaled. Final changed checks
and the distinct configured-fork journey remain pending.
