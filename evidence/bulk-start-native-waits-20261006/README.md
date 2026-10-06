# Bulk Start native-wait diagnostic — prepared, not run

The retained failure shows a Start worker in SQLite close, a notification
worker in SQLite connect, and another worker waiting for the wire lock.
It does not establish a mutex cycle or justify a SQLite patch.

The next authored attempt keeps the installed application and the existing
Start completion check. Its failure handler saves Python stacks first, then
signals its own GDB parent. GDB stops all App threads, saves native stacks,
registers, futex addresses and mappings, and resumes the original exception
and fixture cleanup. The 15-second check still fails; the existing 600-second
outer bound is unchanged. A debugger observation is not a performance result.

GDB must launch the App: host Yama policy refuses an arbitrary sibling attach.
Its inferior has a separate group. Before releasing App code, the existing
controller captures that identity and transfers the group to the recorder's
existing ProcessOwner. BoundedRun retains GDB's original parent wait, drains
and retirement; ProcessOwner covers the inferior group. No second process
manager, namespace, policy relaxation or backend wrapper is introduced.

Python and the statically linked SQLite have symbols but no debug types.
The host libc layout and actual pthread_mutex_lock instructions agree on the
mutex owner field. The capture reports a holder only for a stopped mutex wait
with matching libc bytes and the expected native frame/syscall relationship.
Condition variables, unavailable registers, unmatched libraries and missing
frames stay unidentified. Named SQLite mutex fields remain unavailable without
matching debug information. Raw evidence is retained in every case.

The marker runs on a Python thread holding the GIL. A native thread waiting for
that GIL at this instant is not, by itself, evidence of the original deadlock.
Use the actual lock addresses, holders and complete native stacks to establish
the relationship; do not infer a cycle merely from three blocked threads.

## Integration

Parent owns the current Toad control and physical04. Apply
`existing-capture.patch` to the two existing helper files on the workflow branch
and `existing-controller.patch` to its fresh controller. The patches have only
been prepared here; no peer file or frozen attempt was changed. Bind the actual
adopted source and the fresh purpose to `PREPARED-DIAGNOSTIC.json` before launch.
Its old grant reference is provenance, never authority. No App, attach, build,
test or provider operation was performed during preparation.

Proposed Python sources and the embedded GDB Python compile. The inherited
controller emits its existing warning about a return in finally; this diagnostic
does not rewrite that unrelated outcome mechanism. Runtime confirmation is
pending the fresh reviewed diagnostic purpose.
