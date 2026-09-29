# Canonical owner binding for same-view attachment recovery

Paired with Toad #210. Owner: inbound/source worker. Parent assigned this
canonical source projection; Mendel C3 owns surrounding owner-state contracts,
Arendt owns the complete turn/tracker projection, and Heisenberg #202 owns warm
workspace/presentation custody. Shared methods are coordinated directly before
implementation.

Actual defect: the existing window failed an ACP saved-session attachment against
an old owner. After the owner restarted on the matching runtime, a fresh window
loaded successfully while the original open view stayed stuck. Its backend
canonical source now reads in 67.5 ms for the actual 42 MB saved file; that excludes
ACP transfer and UI preparation/painting.

`ThreadPresentation` currently contains only display values and notifications.
An owner restarting from Ready to Ready can therefore compare equal and suppress
the observation that must invalidate a failed attachment. Carry existing registry
owner/process identity in the same canonical packet. The projection is a value
derived from `RegistrySnapshot`, never a second owner registry or UI PID mirror.
The failed AgentSession reattaches in place when that canonical owner changes;
do not start an owner, resend a prompt, replay an uncertain input, reopen the tab
or discard warm history/editor state.

Acceptance uses the actual installed application, ACP and isolated native owner:
saved source, failed attachment, owner restart, the same continuously open view,
automatic recovery and usable original editor/history. Preserve baseline failure
and correlate source readiness with UI publication. No live user owner or original
history is changed by this worker. This is a draft scope, not a readiness claim.
