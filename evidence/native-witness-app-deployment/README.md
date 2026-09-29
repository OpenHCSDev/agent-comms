# Live paired native witness and application checkpoint

Activated 2026-09-29. Five default launchers and nine idle registered owners now
use the noneditable `runtime-native-witness-app-20260929` installation. The
installed fork heads are agent-comms `f0358239dc04d58a22177439e5d483ec1ee2b276`,
Toad `c28e74f42125d07af4f59d19d18864c89b712674`, and Textual
`609b74bf3d7851bd2c1eff61dc2790e86e1925af`. The native Pi package remains
the previously reviewed `9213ee71479d1b20` build. Exact distribution provenance
is in [imports.json](imports.json).

This installs merged core382/383/384/386/387/388/390/392 and Toad181/182/183/
184/186/187 on the current route. It is a useful checkpoint, not closure of
the remaining nominal refactor plans or final 50 ms latency target. PR185 sidebar,
core389 HistoryViews and391 wire records remain separate work. The already-open
user Toad process still has its older code loaded; close and reopen `toad-comms`
to get this UI.

## Actual checks

- The installed continuous normal App/Pilot/native/ACP/wire/SQLite journey passed
  with controlled localhost provider replies. It painted saved history, delivered
  a native page while the status store OS lock was held, clicked channel and
  participant rows, retained A/B/A bodies13/17/13 and draft/Document/undo with
  raw reads0/0/0, held idle preparation60→60 and provider requests3→3 with no
  pending work, exercised scroll velocity/End, and physically opened a fork
  before its first reply. Channel working/responded feedback and automatic
  author observation passed. See [installed-summary.txt](installed-summary.txt).
- The 12 installed native selected execution, input lifetime and queued input
  cases passed in79.98s. The actual physical Ctrl-Q rejection/retry path also
  passed: painted filesystem error, app stayed open and dirty, original draft
  retained, second Ctrl-Q persisted the edit and exited with one SDK input.
- [Activation](activation.json): nine owners were idle before the cutover; all
  thread metadata except process identity was equal after restart. The route,
  native package, durable stores, native histories and UNKNOWN dispositions were
  retained. No original input was replayed. All five launcher links aligned.
- [Nine fresh ACP loads](live_attach.json) passed with zero prompts. The normal
  default Toad app painted saved #comms/#nra/#openhcs history, matched model and
  thinking selection, and returned to a native tab with draft/Document/undo
  preserved. [Navigation result](live_navigation.json) records the read-only run.
- The exact reported OpenHCS inter-agent message was read-only verified in the
  new installed Toad UI: its #openhcs row painted “Responded (1)” and disclosed
  `openhcs-architecture-memory: Responded`. The bus remained unchanged. This
  confirms sender-side status and a recorded recipient reply. It does not prove
  that inbound channel messages reliably appear in each recipient's tab or
  unread presentation; that user-reported path remains under investigation.
  The old open Toad window must be reopened for the newly installed UI.

The private native/ACP logs, GUI screenshots, preserved failure diagnostics,
one-use operator, raw fixture state and proof backups stay local. The operator
was retired after the fresh ACP and UI checks. No full CI result or final latency
claim is made; CI is deferred by owner direction. Root and home free space still
trigger the resource warning, so subsequent tests remain bounded and serial.
