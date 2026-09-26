# Core maintenance admission prototype (default OFF)

This branch is **not** a live maintenance barrier. It controls only processes
importing this code; old ACP/CLI/workers, Toad reconnect, and external native
clients can bypass it. Do not enable it on a live root or claim a safe restart.

`MaintenanceBarrier(registry_path)` is **read-only** in the installed package.
It recognizes two private phase witnesses in that wire root; incomplete,
malformed, stale-generation or damaged enabled witnesses deny admission. A
fresh never-enabled root (both absent) follows the old path. Direct registry
claims and new owner registrations check under the registry lock; owner starts,
ACP/backend sends and the private coordinator/native adapter check under the
wire lock. Existing in-flight turns are not cancelled or replayed.

There is **no production `begin`, `advance`, `_write_unlocked`, release or
reopen API**. A same-UID Python caller can import the package and would gain
an accidental permanent host-wide denial lever if it exposed one. The only
marker-first, parent-fsynced phase writer is
`tests/maintenance_control_fixture.py`, used solely on disposable roots to
exercise closed phases, crash/UNKNOWN, CAS and native-send races. That fixture
is never installed and is not an operator ACL. `admit_ingress()` holds the wire
lock through a synchronous new-client spawn; there must be no `await` inside
it. `read()` is a snapshot, not atomic permission to spawn.

**Still absent:** a real phase/control plane, authenticated OS-separated
operator, protected root/ACL, safe release/reopen, durable old-client exclusion
attestation, and complete Toad ingress authority. Same-UID arbitrary code can
otherwise mutate a writable wire directory outside this package; this
foundation makes no claim to resist hostile same-UID filesystem access. Those
gaps require a reviewed cross-package contract and an externally authorized
full-ingress maintenance window. Never enable/install against the live root
or infer global safety from the disposable fixture.
