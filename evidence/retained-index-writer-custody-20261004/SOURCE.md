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

Installed acceptance remains pending a fresh, exact THIN540 purpose. Current
retained656 product is enough for tool-only boundary controls; no Core wheel,
package install, environment, native artifact/copy/build, provider/public input,
source overlay or original UNKNOWN replay is requested. The control must use
original canonical private seed/marker/lock/index machinery, retain original
wire/settings/auth/UNKNOWN bytes, attest inherited-FD rebuild and refusal at
actual schema/marker boundaries, and join original children. Any unavailable
old-schema installed donor limits cross-version claims; no hand-authored old
schema, fake controller or mock response is substituted.
