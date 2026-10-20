# Process-owned maintenance (P281)

The barrier stores the holder's PID and kernel birth time through the existing
`ProcessIdentity`/`FieldCodec` implementation. Names are not identities. READY
records describe completed work and do not depend on the old operator staying
alive.

A non-READY read checks that exact process. Recovery compares the observed
record under the existing registry lock, writes `dead_holder_released` with the
dead PID/birth time, and refuses the detecting input. The following input uses
READY behavior. The asynchronous ingress borrows the asynchronous registry
lock; neither path upgrades an admitted shared wire descriptor.

Live installs run in a separate session with private file-backed output, so
losing the calling session does not kill the installer or break its output
pipe. The worker records its own identity. Rehearsals remain synchronous.

`TodoStore.writer_connection` admits all todo writers before opening SQLite,
including commander first-call, hunt, overdue and reconciliation writes.

One registered carry converts version-1 READY witnesses. An old active record
contains only a name: its holder's death cannot be established. The carry
refuses that record rather than fabricating a PID or retaining a name fallback.
The runtime reads only version 2.

## Delivery checklist

- [x] Implementation checkpoint pushed on `fix/p281-maintenance-holder`.
- [x] Production AST inventory: 381 Core modules and 39 tools; no parse omissions.
- [x] Migrate all discovered producer and test callers off string operators.
- [ ] Focused checks, tools ratchet and src ratchet against `origin/main`.
- [ ] Copied-bus live-phase proof using existing installed Python; no build,
      environment creation or `uv sync` is authorized for this proof.
- [ ] Prove calling-session death leaves the worker alive, then SIGKILL the
      worker mid-drain; record first refusal, second acceptance and release JSON.
- [ ] Open P281's PR through the commander's canonical PR command.

The inventory includes declarations, inheritance, imports, writes, decisions
and references throughout both production roots. Generated test child programs
are additionally located by source search; AST does not resolve their runtime
imports or prove behavior.
