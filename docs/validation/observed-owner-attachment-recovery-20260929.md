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


## Reviewed installed checkpoint

Nine production lines deleted relative to main 7fc. Toad #210 owns the paired
recovery consumer; #425 owns canonical turn/input lifecycle. The original failed
load command captures RegistrySnapshot full OwnerIdentity/ProcessIdentity before
proxy subscription. Its FieldCodec FailedSessionLoadAdmission disposition is
carried through official RequestError.data and cannot resolve/replay. Accepted
attachments retain their ORIGINAL successful load response proof in the same
WitnessedSessionLoadAdmission family; no queue admission counter or prebind
observation is used for owner replacement. Replacement uses strictly newer generation for the same thread,
including reused PIDs, then strict fresh full owner/process validation. No
current-owner mirror, new store, compatibility decoder or input replay.

The actual installed paired recovery receipt is committed in Toad #210 at
`evidence/owner-attachment-recovery/installed-receipt.json`: healthy alpha,
physical beta row click, actual old-owner load failure, replacement, same open
view recovery (4.628 seconds), preserved editor Document/undo, explicit new
reply painted and stopped-owner read-only load refused without starting it.
Exit 0; zero provider requests during repair; no original input retried.
Installed Core 7cd4ee8b; the only later source change 05a6fbe3 moves the unchanged
full process check inside its RequestError boundary. Required debt ratchet
against current main passes with zero positive measures. CI is deferred.

T5 outbound/source hot invalidation remains scoped to Core #430 and Toad #215;
it does not delay this useful recovery checkpoint. Parent owns global paired
installation and quiet cutover. No worker global install or live owner mutation.


## Counter-domain review correction

The preceding installed receipt establishes failed-load recovery. Review found
that its accepted-path comparison used QueueScope's admission generation against
RegistrySnapshot's owner generation. Those are different allocation domains;
that comparison and its consumer are deleted. Core 3e4e0c88 captures one original
registry binding before proxy subscription and includes that same witness in
successful and failed load records. Both derive replacement through the shared
WitnessedSessionLoadAdmission capability. Toad consumes the original successful
record for BOTH new/load responses; unsupported external responses carry no
replacement authority. No refreshed last-owner field or second witness path.

The concrete owner=950/admission=914 same-process negative, same-PID/new process
birth and owner generation, rename, unavailable and missing original-witness
checks pass. Required ratchets remain zero. The exact affected installed paired
journey with accepted-record assertions is next after the active #425 fixture;
this new accepted-path closure is not yet claimed live-verified.
