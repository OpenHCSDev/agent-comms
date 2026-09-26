# Core maintenance admission prototype (default OFF)

This branch is **not** a live maintenance barrier. It controls only processes
importing this code; old ACP/CLI/workers, Toad reconnect, and external native
clients can bypass it. Do not enable it on a live root or claim a safe restart.

`MaintenanceBarrier(registry_path)` stores two owner-private, parent-fsynced
witnesses in that wire root. The enabled marker is committed before the phase
record and is never removed. Incomplete, malformed, stale-generation or damaged
witnesses deny admission; a never-enabled root (both absent) follows the old
path. Phase transitions take the wire lock then registry lock. Direct registry
claims and new owner registrations check the state under the registry lock;
start/launch/restart and final native stdin writes check while holding the
wire lock. A pause acknowledgment therefore follows any new-code claim or
native send linearized ahead of it. Existing in-flight turns are not cancelled
or replayed: the maintenance operator must inventory and drain them separately.

`begin(operator)` closes admission in `draining`. `advance(receipt, "paused")`
and `advance(receipt, "installing")` require exact generation/operator/nonce
and remain closed. Crash or parent-fsync UNKNOWN leaves a visible closed state
or an incomplete witness, never an implicit rollback to default OFF.
`admit_ingress()` holds the wire lock through a synchronous new-client spawn;
there must be no `await` while inside it. `read()` is only a snapshot, never
an atomic permission to spawn.

**Deliberately absent:** no release/reopen API, no operator ACL, no durable
old-client exclusion attestation, and no Toad ingress authority. They require
a reviewed combined cross-package contract and an externally authorized
full-ingress maintenance window. Same-UID arbitrary code can modify the
control plane, so a self-declared operator name is not authentication. Do not
use `begin` against live wire state until these gaps are resolved; isolated
provider-free tests operate only on disposable roots.
