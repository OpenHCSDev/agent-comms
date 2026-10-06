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
# Peer opening: unanswered ACP initialization

The returned diagnostic02 passed the first bulk Start checks: each selected
owner started once and both were alive. Opening peer then timed out at the
original 15-second session-settled wait. Its log contains only outgoing ACP
initialize; neither an initialization response nor session/load was reached.
The initialize log precedes the StreamWriter write, so it proves neither child
consumption nor successful server dispatch. No retained stack locates the child.

The relevant owner chain is AgentProcess.start -> admitted_spawn -> AttachedChild
with StreamingChildStdio and shell command decoding -> AgentSession.initialize
waiting for the response. AgentProcess.communicate owns stdout dispatch; its
session failure/closed-startup paths settle the session. On the Core side,
acp.main validates the selected private route/package before constructing
CommsClient and entering run_agent. SessionLifecycle.initialize merely supplies
capabilities; it does not load a thread or launch a model. A preflight, constructor,
pipe or dispatch stall therefore remains possible; package hashing and SQLite
deadlock are not established causes.

Prepared correction, source only:

- `peer-startup-failure-capture.patch` moves the existing timeout observation
  into `until`, retaining the same predicate, 15 seconds and propagated failure.
  Menu completion and all three peer attachment waits use that owner. Unrelated
  waits keep their existing behavior. No startup retry or backend change.
- The existing capture exports AgentProcess runner/session await chains and
  actual AttachedChild identities. Under the explicit debugger diagnostic only,
  it synchronously completes `native/startup-processes.json` from the existing
  process-group owner before the marker. Normal capture never scans groups.
- `native-waits-peer-startup.gdb` shares the existing native collector between
  the App and its recorded ACP groups, including the shell/ACP distinction.
  It checks each birth/group identity, observes native threads, attempts `py-bt`,
  detaches those additional inferiors, then resumes the original App failure.
  The App remains the actual ACP parent and cleanup owner. Child observations
  are sequential; they are not a claim of one simultaneous all-process snapshot.

This corrects the missing capture boundary, not the unresolved feature defect.
Compilation passed for both patched sources and the embedded collector Python.
No import, test, App, attach, native body read or new attempt occurred. Existing
scripts, diagnostic02 receipts and its completed return remain unchanged.

Future binding must select this patch/collector and the new synchronous descriptor
output. Yama is currently scope 1: this collector's original GDB is the App/ACP
ancestor; it does not authorize attaching an unrelated process. Actual attach
permission and CPython `py-bt` availability are unqualified until that separately
bound attempt. Missing Python symbols/helper support is recorded explicitly;
native stacks/registers remain available independently. Any observation failure
must leave the original failed assertion and owned cleanup intact.
