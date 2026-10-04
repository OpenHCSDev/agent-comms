# Retained index writer custody closure

Base actual main c060ec2d7101d3884af86f85e44eadb02d14f871. Adopted exact
`tools/cutover/retained_index_writer.py` bytes from parent432 head67004426
(original3716e4c1, later335/739 reconciliation). Only this tool changes;
production, schema, native, dependencies and other operators do not.

WireLog is the existing read-only constructor and owner of original lock,
metadata and bus bytes. The writer no longer constructs the full Comms graph
(Registration, MessageBus, Goals, owner lifecycle, settings and presentation)
only to reach service.bus.log. No new owner/reader/state/custody path. Original
WireLog.locked validates the current existing schema/certificate; the same
StoreLock.descriptor is passed via argv/pass_fds. Target declaration mismatch,
root/seal refusal, target inherited-inode/lock check, bus hash and marker identity
checks are unchanged. Original schema/codecs/UNKNOWN proofs stay authoritative.

Consumers are RetainedIndexCutover.after_stopped and the historical consumed
PublishOpenhcsRecovery.after_stopped. RetainedRoutingCutover overrides its
writer/installer and inherits only the original lifecycle; no related operator
needs a changed signature. The publication hash gate remains meaningful. No
old one-use operation/public root is executed or replayed.

Before/after source evidence uses original NRA Package/FunctionFacts across
production/tests/tools, reports omissions explicitly, and reads complete
WireLog/StoreLock/installer/consumer semantics. It does not prove arbitrary
dynamic resolution. Parent's seven reset/file-custody controls are not writer
acceptance. Historical556 receipt demonstrates earlier553→556 inherited-FD
rebuild followed by retained owner startup, before this constructor change;
it is retained historical evidence, not qualification of this new source.

Actual installed acceptance completed in 8.917681585997343s under the original
Bohr661 execution-only grant2e9939ef. The unchanged656 THIN540 package loaded
from its original installed path; all347 assets/94 original nonCore keepers and
69 metadata files across10 distributions stayed exact. Historical92 keeper
subset is not relabeled as94. No wheel/install/dependency/native/provider work.

Two fresh canonical seeds through the existing seed tool supplied independent
missing-seal and wrong-schema derived negatives. Actual writer same-schema,
root, seal and wrong-schema refusals changed no further file bytes. The original
installer refused unrelated descriptors and wrong roots, then rebuilt the real
current index under the original inherited writer FD. Frozen sender/audience,
wire/registry/settings and marker identity/admission stayed unchanged. Controller
and all9 Python children exited; no groups/sockets remain. Receipt/raw negative
traces and source proof are retained in installed01 and WHOLE-HANDBACK.json.

This is the current-private custody boundary, not a new cross-version writer
restart. Three exact historical old-schema interpreters are absent. The earlier
556 positive remains historical; no old DDL/codec/environment/controller/mock
is manufactured. Original saved/private/public stores and UNKNOWN inputs were
not read or modified. The consumed public operators were never executed.
