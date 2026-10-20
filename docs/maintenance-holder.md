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
- [x] Eleven focused real-process/ingress/todo/carry checks passed.
- [ ] Tools ratchet and src ratchet against `origin/main`.
- [x] Copied-bus live-phase proof with existing Python, without a build,
      environment creation or `uv sync`.
- [x] Calling-session death left the worker alive; SIGKILL of its job mid-drain
      gave a named `held` refusal, then an accepted and retained second message.
      The release record retained the dead PID/birth time; original bus unchanged.
- [x] Draft PR #2072 opened through the commander's canonical PR command.

## Installation wiring follow-up

Normal and unchanged live installs now use the selected interpreter to refresh
`RuntimePublication`. That existing owner publishes console/skill links and
calls the declared `Harness.configure` implementations; repeated refreshes are
safe. Consumers no longer skip refresh merely because the pointer is current.

Actual installed programs, with a private HOME and candidate publication code,
verified every declared hook on PATH/configuration, including P275 SessionStart
and P285 UserPromptSubmit. SessionStart exited zero with all 45 packaged files;
repeated refresh preserved user settings without duplicate hooks. Production
configuration and services were not changed.

This does not yet fence external bridge watches or session hooks across mutable
checkout/schema updates. The reported transitional-reader failure remains
unfixed; publication success is not acceptance of that separate lifecycle gap.

The inventory includes declarations, inheritance, imports, writes, decisions
and references throughout both production roots. Generated test child programs
are additionally located by source search; AST does not resolve their runtime
imports or prove behavior.
