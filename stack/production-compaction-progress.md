# Production compaction successor

Base: merged PR48 `d0ced47fbec39ac4d110c9f7395442524363871d`.
This is **not deployment approval**. The adaptive trigger remains disabled.

## Default-off ACP adaptive candidate (combined review and acceptance pending)

The opt-in `CommsAgent` path now admits only one *fresh direct original*
`acp:` input on an active claimed goal turn. It reads selected-model usage and
Pi's effective trigger, captures the owner/native/ingress source before a
bounded Pi-native summary request, then retires the idle manager, journal-CAS
commits once and projects exact-ID local metadata before the ordinary native
input send. The direct original remains UNKNOWN/unbound until its own matching
native start; a correction or changed settings/model/source refuses the commit
without replay. Pending outbox and strict fresh reopen remain unchanged. A
native split-turn cut is skipped because this writer has not separately proved
its retention semantics. A detached provider strategy exists but was **never
exercised against a provider** here; it is now refused by the default adaptive
owner-turn route until live selected-process parity is established. Its code
remains time/output bounded, disables provider retries and admits at most four
Pi-native requests, but those limits do not substitute for route proof. Explicit
constructor opt-in defaults OFF; no launcher or live deployment enables it.

Current provider-free tests include actual ACP claimed-turn → synthetic
summary → pinned native CAS → metadata → one original input bind/start, and a
correction/no-dispatch counterpart. The post-main focused source suite passed
**155** in a clean identity environment before the final settings/model-source
hardening; final hardening's owner/commit/runtime/publication suite passed
**66** on real `/var/tmp`. Black/Ruff/mypy and diff checks pass. The attempted
uncleared run with inherited live agent identity failed six tests including
unrelated ACP baseline controls; clean-identity rerun of those six passed.
**No fresh full combined source/wheel run or independent exact-head clearance.**

Adversarial read-only audit identified live project-trust, custom-model/auth,
file-operation/usage metadata and settings-race gaps. This candidate refuses
adaptive payment if project settings or global/project custom model
declarations exist; source capture and commit compare bounded settings/model-file
revisions. Independent exact `9aef5b8` reviews found the default-OFF owner
admission narrowly sound but **NON-CLEAN for activation**: the Pi result's
structured file operations and usage were lost, the selected live Pi model/auth
route remains unbound, and no supported production worker opt-in exists. The
ordinary native hard-context guard is never disabled.

### File-operations/usage corrective successor (pushed; NOT accepted)

Exact `872fb075f7087e69bc22d91789aaa9a3f8ea76b0` carries Pi-native
`details.readFiles/modifiedFiles` and bounded provider usage through the owner
callback, durable metadata digest and verified single-shot child into the native
compaction entry. The independent exact-head review identified a binding gap:
`metadataDigest` is journaled but absent from the native commit marker and
reconciliation. A provider-free transport-fault injection changed `readFiles`
between journal intent and the genuine pinned native write; the operation
reported committed although the journal digest and persisted metadata differ
(`/var/tmp/pr95-872-metadata-binding-probe.log`). The final scoped review is
`/dev/shm/pr95-872-fileops-usage-independent-review-20260926.md` (SHA256
`38a3f457be41996fa8e58a4e60efe67f4cf6dea0f0244715c611d262df95cbbd`).
It confirms narrow positive native preservation and loss/reconciliation checks,
not combined clearance. The mismatch does **not** establish ordinary provider
tampering, but prevents claiming metadata is commit-bound.
Treat exact `872fb07` as NON-CLEAN for metadata integrity; no activation or
merge. A subsequent **corrective candidate, not yet independently reviewed**
adds a versioned cross-language metadata digest over UTF-8 file paths, safe
usage counters and IEEE-754 cost bytes. Python persists it in the intent and
passes it through the exact native commit marker. The disposable pinned native
manager checks the hash against actual details/usage under the write CAS and
checks both marker and persisted fields under locked exact-ID reconciliation;
a successful native receipt echoes the digest and Python compares it to its
durable intent before claiming committed or enqueuing metadata. The single-shot
helper requires the three-field marker. Existing two-field
native fixture markers remain supported, but the production bridge always
requires the digest. A trusted-transport fault changing either fileOps or usage
now returns UNKNOWN with **no write**; explicit no-write reconciliation is
required before any fresh operation. A trusted-transport fault that changes
both details and its request digest may make a native write, but the independent
receipt/intent comparison remains UNKNOWN, with no publication or retry; exact
reconciliation against the original intent remains UNKNOWN. Test-only
post-write changes to persisted fileOps, usage or marker likewise leave
reconciliation UNKNOWN, not committed. Provider-free targeted
positive Unicode/floating-cost, native carry-forward, ACP adaptive and
three-round outbox cases passed serially; full source/wheel/combined review
remains open. A new disposable package was made from the prior reviewed copy;
no installed or live package was changed. Native Pi's
**next** `prepareCompaction` now sees the preserved file-operation lists in a
provider-free two-round control; the ACP synthetic owner success control
asserts file lists and usage too. A disposable copy of the pinned Pi package
with only the changed helper has complete-tree digest
`739c00b7d1e1914bd54815477a1ad593aca0f731229250704bc8ee312d9c7d71`;
no installed/live package was edited. Four new narrow fileOps/ACP tests and
three real SIGKILL cancellation cases passed independently. A larger clean
serial focus had **36 passed, three destructive cases deselected, one Node
fixture startup timeout after five seconds** during severe host memory/IO
pressure; an earlier full focus timed out and is **not** a passing run. The
fixture startup ceiling is being increased for later resource-safe rerun.
Fresh independent exact `06dc706` review is **scoped CLEAN for metadata
binding only** (`/dev/shm/pr95-06dc-metadata-independent-review-20260926.md`,
SHA256 `65022a984348997d40d43d600e1f91cdce323dda9bee27992dad7218b1e5d944`).
Its separately failed reviewer attempt produced no verdict and grants nothing.
There is still no successful full combined source/wheel clearance. A later
uncommitted fail-closed trigger correction refuses the default detached paid
summarizer on selected turns because it cannot bind the selected live Pi
model/auth/baseURL/extension/project route. Synthetic injected summaries remain
provider-free test seams; worker and stdio entrypoints remain default-OFF.
`adaptive-activation-parity.md` records the disjoint in-process proof design.
Remaining blockers: bind the actual live model/auth/
extension/CLI route and project-trust parity, implement reviewed human/operator
opt-in only after clearance, demonstrate provider semantics, and run fresh
resource-safe full source/wheel suites and combined review. No activation.

## Selected idle-Pi readiness and durable operation reservation (not activation)

An independent exact `2256e2bc…` Pi-side phase-1 review is **scoped CLEAN only**
for the old `05774aeb…` prompt-preflight false-idle race
(`/dev/shm/pr95-225-review-R5v8GP/REVIEW.md`, SHA256
`076a86f0775ad47993fa3aa0b0ac536cc1473c15a4cd09dc9b1ffc644a21f800`).
The old bytes remain NON-CLEAN. The new dry-run `ready` explicitly reports
`UNVERIFIED_NO_AUTH_RESOLUTION` and cannot authorize payment or native commit.
No Pi-side bytes have been integrated into this branch; phase 2 has no reviewed
implementation or provider authorization.

Exact `47c8e70` Python-only default-off selected-summary ledger was independently
**NON-CLEAN P2**: post-COMMIT parent fsync failure on native link or pre-start
clean decline raised UNKNOWN but left a terminal-looking row which passed ACP
final send, including after reopen. The native commit in the link probe was
durably committed; fault is send admission, not proof of lost provider output.
Exact successor `3ae8e1d` independently cleared this narrow terminal-fsync
safety defect, but was **NON-CLEAN P2 migration/availability**: its new
all-status unique index failed on valid predecessor multiple terminal rows and
denied unrelated sessions under the wire root. The next conservative successor
retains the historical partial index, transactionally forbids new reservations
when any selected row exists, and keeps **every** selected-summary row
(reserved, UNKNOWN, linked, declined-prestart) as a durable final-input blocker.
Historical duplicate terminals remain preserved and block only their own
session. A linked or declined row is accounting information, **never**
automatic input permission; no exact-ID recovery/owner-scoped handoff exists. The
journal still reserves before any future RPC and requires same-ID native
committed-intent linkage; no provider request or original input is retried.
There is no ACP producer or imported Pi phase-2 bytes. Exact separate Pi
candidate `85ef9e6` is independently NON-CLEAN P2 for noncooperative stream
timeout and buffered output cap, and must not be imported or repinned.
`selected-summary-operation-journal.md` records both exact failures and
remaining gates. Exact `6598755` independently **scoped CLEAN** Python-only
migration and fail-closed selected exclusion: 45 serial provider-free tests
plus real predecessor DB migration, two-process alias race and terminal-fsync
fault probes (`/dev/shm/pr95-659-independent-BO57UF/REVIEW.md`, SHA256
`0aeb5d25f9587607cb5ab44cef13dbc40114ea805937d427f3e3330ff7610518`).
Exact `749a4c3` default-OFF positive admission was independently **NON-CLEAN**:
its private helper could re-mint from a terminal row after post-COMMIT fsync
UNKNOWN, and its transformed prompt digest failed to bind durable original
`InputDispositions.source_text`. The corrective local slice issues a one-use
operation/session/status/source receipt **only on the same terminal update's
returned parent-fsync ACK**, then burns it when minting the process-local
owner/turn/ingress capability. It separately stores/checks the exact durable
original-source digest at reservation and final wire-locked bind before any
stdin write; the transformed prompt digest remains distinct. The linked
variant also requires exact reserved-source digest in committed native intent.
Provider-free fault/fake ACP tests cover both reviewer counterexamples,
owner/source mismatch, fork/process death, one-use and post-bind uncertainty:
focused selected admission/journal/send-gate **45 passed** and adjacent native
journal/dry-run/ACP input+goal **71 passed**, serial/local; Black/Ruff/mypy/diff
checks pass. Independent exact `a6b6974` review initially reported scoped CLEAN for the
previous fsync-UNKNOWN/original-digest probes, but its reviewer independently
confirmed a distinct **NON-CLEAN P1** private status-only mint: callable generic
`_transaction(selected_ack=...)` accepted a raw one-row terminal UPDATE with
no clean decline reason/verified transition, issued a receipt, then bound input
(`/dev/shm/pr95-a6b-independent-4pXfAC/ADDENDUM-status-only-ack.md`, SHA256
`98b466910a3964118d9c6d3d1c81544e22f2ef1d2b1d78d82990b90aeb0c7a17`).
Exact `b2ae982` inherits the defective bytes; its subsequent independent
SCOPED CLEAN is **merge-conflict-only**, not admission clearance
(`/dev/shm/pr95-b2ae-independent-muv0lc/REVIEW.md`, SHA256
`d9131a5b547917694fa1a5aaf28aab4acaad2ffb2ec058df0ff2f14a71240901`).
This narrow corrective successor removes receipt issuance and caller-supplied
ACK scope entirely from generic `_transaction`; only public exact committed
link or clean-decline SQL can register a one-use receipt after that method's
COMMIT + parent fsync returns. Status-only private SQL leaves a terminal row
but no ACK, fails direct mint, and blocks ordinary final send. Local focused
provider-free **49 passed** (`/var/tmp/pr95-selected-closed-terminal-ack-focused.log`,
SHA256 `810e689b7f80b50d6353fdafa801c15030b25069fcf08de64fab96bdd6297c8c`);
adjacent ACP/native-journal/authority/fake guardian **150 passed**
(`/var/tmp/pr95-selected-closed-terminal-ack-adjacent.log`, SHA256
`4f6067e3bf12c2097156d527201502c84743d9082bd28056d46ddc9e221b3a10`).
Fresh independent exact-head review is required. No production ACP producer
exists and persisted linked/declined rows never grant input by status.
Pi phase-2 candidates through `764b69d` remain NON-CLEAN P1 for custom
EventStream terminal/slot authority. A separately reviewed Linux dedicated
namespace guardian prototype exact `9a40d274` received **scoped CLEAN** for
provider-free operation-dedicated fake containment only
(`/dev/shm/pr95-guardian-9a-fresh-nnTPLY/REVIEW.md`). Its two Python source
files and two tests were copied byte-for-byte into a distinct default-OFF
integration slice; `run_selected_summary()` still unconditionally rejects.
Own fake combined tests reserve the SAME operation ID, exchange one fake RPC,
retire the exact child and keep original input blocked on success/timeout;
guardian/combined plus selected admission/journal/send focused **60 passed**
serial/provider-free (`/var/tmp/pr95-selected-guardian-combined-python.log`,
SHA256 `34a9661cace1a171ffb9cb16342cd5a705cd77cfd86391876ab19734bb12081a`). No 90-second hard SLA,
SDK route/auth parity, provider terminal receipt, live selected Pi, semantic
retention, operator opt-in or production clearance is inferred; fresh exact
PR94-merged review and full source/wheel testing remain mandatory. The normal
main `1273f0f` merge resolves ACP's disjoint adaptive/N-K constructor kwargs
and combines `_store_lock` bounded-read options with inherited child descriptor
semantics. Initial serial provider-free PR94 ACP/private-bus and PR95 admission/
guardian integration: **326 passed, 1 skipped**
(`/var/tmp/pr95-pr94-merge-focused.log`, SHA256
`ef1f85449d615ac902c5886655b3616b4a18619720d49d4a4f31058aa134c052`);
adjacent PR94 native-send/disposition/runtime/backend + PR95 authority:
**263 passed** (`/var/tmp/pr95-pr94-merge-adjacent.log`, SHA256
`8370a1f1febb6088310e93e61fab5dbb4a93b91446607f4a8e9ff4966cb67abb`).
No selected provider operation has been started.

## PR94 private raw writer × selected-row exclusion (default-OFF draft)

The exact `b2ae982` merge review independently scoped-cleaned conflict
resolution but expressly left PR94's **distinct private N/K raw writer** outside
the PR95 selected-row final-send gate. A disposable exact-`6096df9` baseline
with the same provider-free selected-row test adapted to its old zero-argument
boundary failed **all 3** reserved/UNKNOWN/terminal cases: the fake crossed
the old raw-send boundary before raising its deliberately injected fake model
error (`/var/tmp/pr95-private-crossroute-before.log`, SHA256
`b026718bdeb446d57004ddc289e28132e8bfb7d21ea27d44dd80f1ccfc2bf811`).
The corrective draft supplies the exact native `get_state.sessionFile` path to
`_native_send_boundary`, retains its existing shared wire→bus→registry→store
and one-use prompt exclusions, then holds the selected journal BEGIN IMMEDIATE
through every raw `os.write`. **Any selected status**, including apparent
linked/declined without returned ACK, or unresolved native compaction intent
blocks the same saved file. A direct competing journal reservation cannot
commit until that write scope ends; another session is not blocked. A resumed
saved file renamed before send is refused, and ordinary owner/rename/cancel
cases remain under PR94's prior exclusions. Fresh paths may not yet have a
header at `get_state`; their canonical path is still fenced through write.
Local serial provider-free PR94 native/selected/journal/fake combined **167
passed, 1 deselected** (the pinned-Pi import test was deliberately excluded),
`/var/tmp/pr95-private-crossroute-focused.log` SHA256
`b5abb7d9a118cfe93885b71358a516c3f34600c8cda0459209c6def68fe45d8b`;
adjacent ACP/private-bus/send admission **148 passed**,
`/var/tmp/pr95-private-crossroute-adjacent.log` SHA256
`df047da9e7fba7a88e7629d80f36d4db0da578cc8a7cfad1c4309d5b6c2f2a0c`.
This is a one-way raw-after-selected safety fence, **not** a proof that a
selected reservation AFTER an uncertain PR94 raw input is refused by the
separate PR94 runtime store. That reverse-order cross-store gate and real Pi
terminal/route parity remain explicit pre-opt-in blockers; no provider spend,
live input or production activation occurred. Exact pushed `8d1bcd8` received
independent **SCOPED CLEAN** for raw-after-selected only
(`/dev/shm/pr95-8d-independent-utVmO9/REVIEW.md`, SHA256
`e6652111c502d14b3444a56e7acf30a3185dc34813f5f3bc3fde9f1da8575010`);
that verdict does not cover the following successor.

### Conservative raw-first UNKNOWN marker and legacy coverage floor (safety draft)

PR94's existing `native_runtime_inputs` row reserves input ID before Pi starts
but leaves `session_file=NULL` until an authenticated live result, so it cannot
bind a crashed/UNKNOWN raw input to the exact saved file. A distinct safety-only
successor introduces immutable `private_raw_inputs(input_id, session_file,
status='unknown')` in the SAME durable selected journal. The verified PR94
isolated writer registers this exact-session marker **with COMMIT and parent
fsync BEFORE `os.write`**, then rechecks the marker and selected/native rows
under journal BEGIN IMMEDIATE through the bytes. If the marker fsync is UNKNOWN,
no raw write is attempted and the reserved PR94 input is not replayed; a
visible marker remains a selected reservation blocker. Crash before write and
after fake local write before result also leave the marker after reopen.
Ordinary subsequent PR94 raw input IDs may proceed on the same session, and
other sessions remain independent. Marker rows are **never cleared** by a raw
ACK, fake result, or terminal-looking row; exact child retirement/native
input-ID settlement is not yet available as a reviewed clearance authority.

Pre-install PR94 saved sessions can lack markers despite old raw/UNKNOWN input.
An exact-`8d1bcd8` disposable provider-free baseline **wrongly allowed** an old
private saved session with no marker; the successor denies it
(`/dev/shm/pr95-private-legacy-floor-probe.py`, SHA256
`769a8cda39ae26d6b1bed3660db039496084b5938f2149818f88552663eacd3b`;
before/after logs `/var/tmp/pr95-private-legacy-floor-{before,after}.log`).
Therefore `reserve_selected_summary` now refuses **all canonical private
`root/native-sessions/**` paths** before any selected side effect: there is no
trusted new-session epoch/coverage issuer and no guessed backfill. Existing
pre-floor selected rows still block raw write. Alias/symlink path resolution,
old-import no-marker denial, post-COMMIT parent-fsync UNKNOWN, and crash/reopen
controls are provider-free. Bounded serial split suites: PR94 runtime/raw
**104 passed, 1 pinned-Pi-import test deselected**
(`/var/tmp/pr95-private-reverse-runtime.log`), selected journal **27 passed**
(`/var/tmp/pr95-private-reverse-journal-only.log`), admission/guardian **43
passed** (`/var/tmp/pr95-private-reverse-admission.log`), adjacent ACP/private
bus **120 passed** and send/private N/K **28 passed**
(`/var/tmp/pr95-private-reverse-adjacent-core.log`,
`/var/tmp/pr95-private-reverse-adjacent-tail.log`). A final exact negative
subset **11 passed** (`/var/tmp/pr95-private-reverse-marker-negative.log`, SHA256
`b39ea8c15f48d3b13805f46b7f143d3acb036ee369f32d9629ae4b1ebcd452bd`).
A larger monolithic run stalled and was stopped at its hard bound; split runs
passed and cannot be represented as a single full-suite pass. This safety successor is NOT usable
positive selected compaction on private sessions; separately reviewed exact
file dev/ino + owner/new-session epoch migration/coverage and authenticated
terminal settlement/reap must precede opt-in. Fresh exact-head review remains
mandatory; no provider spend/live input/activation.

## Normal PR104/105 main integration and socket incarnation (combined review pending)

After clean `dadb7c4`, normally merged main `a6b43fec` (including PR104/105)
without rebase/reset. The sole conflict in `stack/bin/prepare-pi-native` was
resolved by applying PR105's reviewed proof-journal headroom patch after
input-recovery and before the PR95 manager/import-boundary patches. The
manifest's complete-package tree commitment was recomputed against a
**disposable copy** of the prior reviewed pinned package with exactly that
proof patch: `7c5febb9e0671db69789554f6ce1d96f97ef2131b9fc51932c8146f0238f7e76`.
All manifest file pins, native package tree verification and the canonical
launcher admission passed. No installed Pi/live session was mutated. First
merged full-suite attempt failed only because an unrelated ACP config-option
update from the background drain appeared in a metadata-only test's recipient
log; manual publication controls now cancel that unrelated drain and assert
specifically on `compactionPublication`. Isolated corrected publication plus
PR104 owner-followup controls **22 passed**. The new socket-incarnation
pretransport/postdelivery controls and entire publication file **12 passed**
on `/var/tmp`, plus **19 passed** in the extracted wheel for socket
publication/owner worker/settings. `/dev/shm` user-quota instability remains
a test-environment limitation. A merged full-suite attempt stopped after an
unrelated ACP backend-process PID-file read saw an empty in-progress file;
that one control passed isolated, but this is **not** a successful full-suite
result. An expanded wheel attempt was inconclusive after unrelated native
test startup, while the focused wheel subset passed. Full merged suite and
independent combined review remain required.

## Read-only selected-settings trigger seam (not an ACP caller)

`owner_compaction_settings.read_compaction_decision` now obtains Pi's effective
global/project compaction settings through Pi's own `SettingsManager` merge and
`shouldCompact`, but a read-only bounded storage adapter avoids settings lock
files and rejects malformed, symlinked or changing configuration. Private
`PI_CODING_AGENT_DIR` provider-free controls cover strict threshold, disabled
settings and no writes (**4 source/4 extracted-wheel passed**). See
`compaction-trigger-settings.md` for the outstanding selected-model binding,
effective kept-window preparation and model/settings source recheck. This
helper has **no ACP call site, provider request or commit authority** and does
not change the independent hard-context protection. Exact prior full suite
at frozen `7e0ef0b` passed **1729/62 skipped** on `/var/tmp`; the new seam
has not had a fresh full-source rerun yet and cannot be promoted to clearance.

## Scoped eedf reviews and subsequent local-transport corrections

Independent exact `eedf7993` reviewers returned **narrow CLEAN** only for
(a) cancelled idle-child retirement/strict reopen and symlink alias legacy
writer denial (`/dev/shm/ac-pr48-retire-correction-independent-review-eedf799-20260926.md`)
and (b) first→second canonical session handoff fencing/metadata pending
(`/dev/shm/pr95-eedf-independent-review-20260926/PR95-EEDF-LOCAL-PUBLICATION-INDEPENDENT-REVIEW.md`).
They do **not** clear the aggregate successor or adaptive production caller.

The later exact `483d304` client-only binding correction checks the ACP
client/thread at the actual transport entry as well as around outbox
observation. Distinct controls cover pretransport wrong-client refusal and
post-delivery pending/no false ACK (delivery already happened). A separate
liveness successor places a finite deadline around the local transport and
**joins** cancellation cleanup while still holding the per-wire identity
fence. Its provider-free controls cover timeout, owner-task cancellation,
and partial multi-listener delivery: identity mutation is refused while the
transport is stalled, succeeds only after cleanup, and the exact row remains
pending with no native abort or retry permission. An uncooperative arbitrary
client that suppresses cancellation deliberately retains the fence until
verified owner-process termination. The first concurrent full-suite attempt
stalled without a result and was preserved; a sterile verbose rerun passed
**1724 passed/62 skipped** with three nonfatal asyncio subprocess destructor
warnings. Extracted-wheel subset **30 passed**; Black/Ruff/mypy clean.
This successor requires an independent exact review before widening any
clearance.

## Owner native commit cancellation join (future ACP caller still open)

Exact `37796d5` independent review cleared only the read-only pre-summary
source and corrected outbox mark semantics, while identifying a forward
integration hazard: unshielded `asyncio.to_thread(bridge.commit)` could outlive
owner cancellation and release the outer ACP turn lock before native mutation
settled. Exact `26e8393` fixed repeated **outer owner** cancellation but an
independent fake and real pinned-native review found **NON-CLEAN** inner-task
cancellation: `Task.done()` became true while its `to_thread` OS worker still
held one unresolved native intent; the outer lock was released too early.
The new corrective successor retains the actual `concurrent.futures.Future`
for a single native worker (not a cancellable named asyncio Task), shields its
async wrapper, and joins real worker completion under repeated owner/wrapper/
all-tasks cancellation before releasing the turn lock. Provider-free fake and
actual pinned-native controls hold the writer after durable intent, verify a
second turn cannot enter before quiescence, then corrupt saved disk and prove
no fresh fake RPC launch before strict reopen. Prior26e839 NON-CLEAN remains
attached to its original bytes until this successor receives exact review.
Focused fake/pinned-native shutdown controls **6 passed**, extracted-wheel
owner/publication/reopen suite **61 passed**, Black/Ruff/mypy clean. The full
postcorrection suite has not yielded a valid result: one run stalled in an
unrelated publication test, and a second hit `/dev/shm` user disk quota
(`sqlite3 disk I/O error`) despite filesystem free space; logs are preserved.
Only our own disposable prior test basetemps were removed after recording log
hashes in `/var/tmp/pr95-own-scratch-cleanup-20260926.txt`. Full sterile source
and exact combined review must be rerun before integration. Cancellation is
never interpreted as aborted-no-write or replay authority. Normal integration of main `0887b811…` (including PR100) passed isolated full
suite **1719 passed/62 skipped**, extracted wheel **81 passed**, and focused
Black/Ruff/mypy. A later provider-free three-round integration fixture now
drives the internal owner helper through real pinned native CAS into the durable
outbox and an existing local ACP client, checking distinct exact commit IDs,
metadata-only output, unchanged goal authority and no summary broadcast.
It deliberately injects synthetic summaries and is **not** an ACP production
trigger/call site or evidence of semantic retention. The successor isolated
suite passed **1720/62 skipped** (two nonfatal event-loop subprocess destructor
warnings in unrelated cases) and extracted-wheel controls **82 passed**;
run directories were moved to `/dev/shm` after the shared `/var/tmp`
filesystem temporarily filled during concurrent runs. No production ACP caller/model strategy exists yet; this test
alone is not full runtime clearance. The operator failure classes, exact-ID
reconciliation and conservative no-repair path are recorded in
`compaction-operator-recovery.md` (not an activation procedure).

## Runtime/ACP exact 12050b1 NON-CLEAN corrective candidate (review pending)

Independent reviewers found (1) cancellation before the old manager's
`reopen_required` marker with a live SIGTERM-ignoring child and a fake-RPC
strict-validation bypass; (2) canonical session rebind during awaited local
ACP publication before actual handoff, causing wrong-session metadata delivery
and premature observed mark; (3) a renamed symlink to verified `pi-native`
entering the legacy `/compact` route. Their exact old bytes are NON-CLEAN;
no pinned Pi/provider execution or native mutation was inferred from the fake
controls. This candidate sets the marker before cleanup await, retains a
shielded reap task awaited by every next borrower, resolves launcher aliases,
and protects actual metadata handoff/mark with a separate nonblocking identity
fence plus before/after owner-session checks (not a registry lock over client
I/O). Real-child cancellation/invalid reopen, alias refusal, cross-process
identity and prehandoff tests pass; isolated full suite at frozen `eedf799`
**1707 passed/61 skipped**, extracted wheel **79 passed**, Black/Ruff/mypy clean.
The newer cancellation/post-delivery successor requires a fresh full rerun. See
`compaction-runtime-review-corrections.md`. Exact correction review, final
combined review and activation remain OPEN.

## Owner preparation and outbox fsync-uncertainty successor (combined review pending)

Read-only disposable pinned Pi in-memory loader derives the actual
`prepareCompaction` cut point and native disk witness; canonical owner/ingress
source is captured before any summary request. Internal
`compact_owner_once` accepts a trusted injected summary strategy, retires the
idle persistent child before the native writer and leaves strict fresh reopen
required even if the source recheck refuses mutation. Real provider-free native
tests cover correction invalidation and three sequential commits with distinct
IDs; they prove structural transitions, not provider summary quality. It has
**no production ACP caller/model strategy/automatic trigger**, so adaptive
behavior is not activated. Exact `2107c055` independent outbox review was
NON-CLEAN in documentation scope: post-COMMIT directory fsync denial on
`observe_publication` may raise UNKNOWN while the row is already `observed`,
not unconditionally pending. This successor clarifies pending-or-observed
exact-ID reconciliation and adds a real fault regression. The old bytes remain
NON-CLEAN; no verdict transfers to `12050b1` ACP projection. Bounded focus:
261 passed; full isolated source suite: 1703 passed/61 skipped; offline
extracted wheel: 75 passed. No general OS subprocess sandbox is claimed. See `compaction-runtime-reopen-correction.md`.

## ACP native-commit send/outbox/reopen slices (combined review pending)

A committed exact-ID native outcome now atomically enqueues only metadata in
its durable journal; an existing local ACP owner session projects pending
metadata before the next native send, ACKing locally only after a transport
returns. No listener or uncertain send leaves it pending; exact ID dedups
reprojection. ACP final send boundary refuses any unresolved native intent or
UNKNOWN before binding input, including correction/follow-up paths. Idle native
Pi manager has an irreversible discard-before-external-write method; its next
attempt strictly validates v3 saved disk through the pinned read-only loader,
then compares fresh RPC state identity before any provider send. Canonical
legacy `/compact` fails closed rather than using separately installed Pi as
an alternate unjournaled writer. Provider-free focused 344 ACP/backend/journal/
reopen tests pass; full isolated source suite after local projection
1694 passed/61 skipped. Production owner adaptive call site is still required. See
`compaction-publication-outbox.md` and `compaction-runtime-reopen-correction.md`.
This is not adaptive activation or combined clearance.

## Package-subprocess corrective successor (both designated scoped reviews CLEAN)

Both reviewers retain NON-CLEAN for the **old** `8c6ce7` package admission: update checks
could invoke npm/git for already-installed unapproved sources. New candidate
admits the whole source set before dedup/probes and forbids all three actual
package-manager subprocess sinks, including direct metadata/global-root APIs.
Approved manifest-local install/discovery/check/update remains usable.
Tree `628b68df…`, build `e9f19aa7add2a71c`; 36 provider-free package-process cases,
actual canonical PR77 RPC/ancestor controls, source243/1skip, wheel54 and native/
adaptive contracts pass. See `native-package-subprocess-correction.md`.
Both designated independent reviewers subsequently cleared the exact
`8704c64` corrective package-manager source/subprocess slice **narrowly**
(preflight own copy and sink original-finding continuity). Their verdicts do
not clear runtime/ACP/outbox/recovery/final combined review, which remain OPEN.

## Earlier 8c6ce7 import integration (package slice NON-CLEAN)

After draft `5dbd6fe`, canonical preparation now includes the immutable PR77
production package, native-only manifest extension loader, pre-install package
source admission, synchronous Node resolve/load hooks and in-root compaction
helper. CLI and bridge preload the fence; helper/resource bytes must agree.
Tree `b6d13d86…`, build `26e29f3669b35ce5`; manager remains `10ac30c1…`.
Real canonical `pi install`/RPC proves PR77 registration/status with no unapproved
MCP child under kernel network denial. A reached committed dependency's ancestor
require is refused; old unguarded execution succeeds. Source **243 passed,
1 skipped**, wheel **54 passed**, native/import/adaptive contracts pass.
See `native-import-boundary-integration.md` for exact evidence and limits.
Actual runtime/idle/ACP/outbox/recovery integration and fresh review remain OPEN.

## Earlier blocking independent writer findings

The `9c7445e` writer-coverage review is **NON-CLEAN**. Manager `41a94b37…`
remained affected through `8cf666c` and the main integrations at `98b8770`:

1. A denied `createBranchedSession` destination lock leaves changed manager
   state pointing to a nonexistent destination. Catching the error and then
   appending can report success while creating headerless JSONL.
2. A malformed `setSessionFile` target throws after binding its path/revision,
   retaining the old tree and flushed state. Catching the error and appending
   can write a stale-tree row onto the corrupt target.

The corrective candidate now irreversibly poisons a failed manager, covering
all subsequent mutators and history/witness reads rather than just the first
exception. New manager `10ac30c1…`, tree `7d16eb01…`, build `1684f7d9f014feb8`:
**494 continuation controls pass** on a fresh canonical build; both original
counterexamples reproduce on the preserved old artifact. This is owner evidence,
not by itself independent clearance. Preflight reviewer subsequently reported
narrow CLEAN for exact `3585b0f` (own differential and 114-control subset).
Original-findings sink subsequently independently gave narrow CLEAN for3585b0f
as well (18 own denied continuations and fresh-instance recovery). Runtime
manager disposal/error projection is NOT covered. Exact corrective review goes to both
`pr1-goal-p15-sink-independent-review` and
`pr1-native-goal-preflight-independent-review`. See
`compaction-writer-failure-state-corrections.md`; import closure and runtime
integration remain open.

Independent report:
`/var/tmp/ac-pr48-writer-coverage-independent-review-9c7445e-20260926.md`.
The prior journal/watchdog narrow CLEAN is unchanged. Main `fc417934` (PR77)
was normally merged at `98b8770`; its integration run was **240 passed, 1 skipped**,
which does not include corrected versions of these two still-open negatives.

## Executable authority/lifetime foundation

- `ThreadRegistry.guard_owner_compaction` uses the existing attestation
  checks, but retains the registry lock throughout the caller's critical
  section. Existing `attest_owner_compaction` now delegates to it and still
  returns only a point-in-time audit snapshot.
- Registry lock order is registry → native session writer. Registry methods
  cannot be called recursively inside the guard.
- `_store_lock` yields its descriptor. POSIX release is by **last close**, not
  explicit `LOCK_UN`, so an inherited native child's descriptor keeps registry
  writers excluded even if Python dies or unexpectedly unwinds.
- Internal `run_authority_child` is a single-attempt, maximum-30-second Linux
  transport with an independent pidfd watchdog armed before native exec. It
  inherits authority FDs, kills/reaps on timeout/cancellation, and never retries.
  The watchdog survives parent SIGKILL and receives no authority descriptors.
  See `compaction-durability-deadline-corrections.md` for the independent-review
  findings, exec-gate proof, effective SQLite EXTRA and per-COMMIT directory sync.
- Non-Linux/kernel-missing-pidfd configurations fail closed in this bridge;
  ordinary registry locks and audit attestations retain their prior portability.

Provider-free tests exercise real competing stop/heartbeat/goal registry
writers, exception unwind with inherited authority, positive child completion,
transport timeout, KeyboardInterrupt, and parent SIGKILL followed by child
mutation while authority is still held. These tests use Python children, not
native Pi. They prove the lifetime primitive, **not native commit integration**.

Reproduction (focused suite does not exercise whole-repo coverage):

```sh
TMPDIR=/var/tmp PYTHONPATH=src env -u AGENT_COMMS_THREAD python -m pytest \
  -n0 --no-cov -q tests/test_owner_compaction_authority.py \
  tests/test_owner_compaction_gate.py tests/test_declarations.py \
  tests/test_concurrency.py
```

Result: **148 passed, 1 skipped** on Linux/Python 3.11.
Unsetting `AGENT_COMMS_THREAD` isolates a legacy test which only clears
`PI_AGENT_ID`; it does not affect the new authority tests.

## Executable native commit/journal checkpoint

`OwnerCompactionCommit` now couples the retained registry guard to a single-shot
non-forking Node helper in a **disposable** pinned package. The Python API checks
canonical owner/epoch/turn/goal and canonical saved-session path. It journals a
unique intent BEFORE dispatch, retains authority until child exit and outcome
persistence, and never dispatches a used ID. Verified SQLite EXTRA commits plus
explicit post-COMMIT directory fsync,
parent/ancestor fsync and a unique unresolved-session index preserve uncertainty
across crashes. A terminal outcome is immutable; unresolved intents block new
bridge commits to that session.

The native successor patch adds exact operation-ID/digest stamps, strict JSONL
reads and writer-locked reconciliation. A matching unique entry is fsynced before
reporting committed; absence only proves no-write if the exact captured disk
revision remains unchanged. Changed/duplicate/malformed/mismatched evidence stays
unknown. Removing a stale native lock is never itself reconciliation.

Patch a NEW disposable package only:

```sh
python stack/patch-native-session-writer-prototype.py --production \
  "$PI_NATIVE_PACKAGE_DIR/dist/core/session-manager.js"
python stack/patch-native-compaction-journal.py \
  "$PI_NATIVE_PACKAGE_DIR/dist/core/session-manager.js"
python stack/patch-native-writer-coverage.py \
  "$PI_NATIVE_PACKAGE_DIR/dist/core/session-manager.js"
```

Current resulting manager SHA256:
`41a94b3777ac0ec322f649e3e234836893b8de86085f55a927ce29216205c28f`.
The prior `8ec0b8f1…` journal-only artifact remains unchanged for the independent
`5f50fe7` durability/watchdog correction review; it is the exact input to the
new writer-coverage patch.
No production fault hook was added. The helper rejects receipt fields and
requires an inherited FD with matching stat identity and parent PID. **These
lineage checks do not independently prove a flock is held**; correctness relies
on the trusted Python launch path. They are not a new public RPC authorization
protocol or proof against arbitrary same-UID code execution. The bridge pins
SessionManager bytes at this initial checkpoint. The later complete-package
checkpoint below replaces that weaker bridge-only pin.

Additional provider-free reproduction:

```sh
PI_COMPACTION_TEST_PACKAGE="$PI_NATIVE_PACKAGE_DIR" \
  TMPDIR=/var/tmp PYTHONPATH=src python -m pytest -n0 --no-cov -q \
  tests/test_compaction_journal.py tests/test_owner_compaction_commit.py
TMPDIR=/var/tmp node stack/test-native-compaction-journal.mjs
TMPDIR=/var/tmp node stack/test-native-writer-exclusivity.mjs
TMPDIR=/var/tmp node stack/test-native-auto-compaction.mjs
node stack/test-adaptive-compaction-contracts.mjs
```

Integration tests exercise actual native commit while stop/heartbeat/goal
registry writers are excluded, stale owner/goal refusal, lost result without
resend, no-write reconciliation, outcome-persistence failure, stale-lock
recovery, and real owner SIGKILL after native durability but before journal
outcome. A further deterministic test-only JS wrapper gates the actual native
`appendCompactionIfCurrent` after stdin parsing and lineage validation: kill
Python with the native child still waiting, prove real registry stop and native
executor acquisition both remain excluded, release the native mutation, then
recover the original intent under a fresh owner without resending. Neither the
production helper nor the patched package contains that barrier/fault hook.

The bridge now acquires the existing **executor lifetime** session fence
nonblocking BEFORE registry authority, and inherits both descriptors into the
native child. An already-running backend stream is refused before intent or
dispatch. This executor fence is distinct from the native per-entry writer
fence, which still comes AFTER registry authority. Idle persistent Pi managers
must additionally be closed/reopened by the future runtime integration; they
cannot silently keep an in-memory tree after this external helper writes.

The bridge additionally captures a frozen `CompactionSource` BEFORE summary
preparation: native witness, root identity, owner/turn/goal, and bounded actual
bus/input contents plus file identities. Commit compares those observations
under executor → wire → bus → registry → input → native exclusion, retains all
five Python descriptors in the child, and rejects current-admission UNKNOWN
inputs without resolving or replaying them. Whole-store fingerprints are
conservative: unrelated bus/input movement also declines the candidate. Invalid
or incomplete bus data declines preparation without repair. The legacy integer
correction counter is not used as evidence.

New tests cover raw bus sends, Comms.send and direct input-record races, accepted
queued correction refusal, STARTED-input drift, and pre-summary bus correction
invalidation. Native orphan tests now prove all five outer exclusions survive
SIGKILL. Combined focused suite: **177 passed, 1 skipped**; the seven
native-process race/crash cases passed three repeated runs. No provider calls
were used; the installed package remains unchanged. Subsequent independent-review
journal/deadline corrections raise the focused total to **186 passed, 1 skipped**
(see the dedicated corrections record).

## Native writer coverage successor (disposable only)

The next patch removes load-time newline repair and implicit legacy migration,
rejects corrupt/partial/invalid-ancestry persisted data, prevents post-load stat
refresh from blessing stale memory, and rereads unbound preloaded arrays. Empty
file initialization retains its pre-read revision through the guarded rewrite.
Fork and branch paths now hold their source lock; fork destination creation is
writer-fenced, exclusive, and file/ancestor-synced. The bridge pin now requires
this new artifact, not the earlier journal-only manager.

`stack/test-native-writer-coverage.mjs`: **19 passing controls**, with ten
reproduced assertion failures on the old artifact before passing on the new one.
Tests include actual external-process appends during constructor/switch loading,
held source/destination locks, stale branch refusal, fork partial-write and
parent-sync UNKNOWN denial, and coherent positive fork/branch paths. Fault hooks
are test-only Node builtin/prototype interception, not production package code.

On the same new artifact, native journal, writer exclusivity, hard-context,
manual/parallel compaction, input recovery and adaptive-contract scripts pass.
The two older summary fixtures now accept `PI_NATIVE_PACKAGE_DIR`, avoiding any
need to populate an installed/canonical package path for tests. Main-integrated
Python authority/journal/ingress regression suite: **200 passed, 1 skipped**.
Logs: `/var/tmp/pr48-allwriter-*.log`. This checkpoint preceded the complete-tree
preparation below; runtime integration remained unfinished and no installed
package was changed.

## Complete-package preparation and packaged bridge resources

At the prior provenance checkpoint, canonical preparation applied three native
successor patches (the failure-state correction now adds the fourth) and
verified the entire dependency tree. The CLI wrapper verifies it again before
launch; the bridge checks before journal creation and before each native call.
A single full-tree commitment in `pi-native.sha256` replaces the manager-only
bridge pin. That checkpoint's build was `d45562f846a0afa3`, tree SHA `4a688172…`;
the current corrective pins are listed above.
Preparation materializes only internal regular-file npm aliases as independent
copies, preventing symlink/hardlink patch escape into stock. Node ambient loader
options are removed; the managed-project bootstrap is preserved inside the
verified native package. The helper uses non-forking `env` → Node exec, retaining
the exact watchdog PID and inherited exclusions.

Wheel builds include the canonical manifest and Node helper as package data;
installed code never guesses a neighboring source checkout for these resources.
An offline sdist → wheel build and **51 passing** extracted-wheel package/native
integration tests cover this path, without installing anything. The source-tree
provenance/authority/journal/ingress suite passes **227 tests, 1 skipped**. Canonical preparation was also
run twice in a disposable repository, with actual CLI `--version` and all native
scripts. Unlisted dependency drift blocks both launch and re-preparation, and
ambient malicious Node preload tests prove no marker execution.

Details and trust/rollout limits: `native-package-provenance.md`. Independent
CLEAN of prior durability/watchdog defects is limited to `5f50fe7`/`90c4d57`;
new writer/provenance bytes still require review. Current main `3e1813e` was
normally merged at `409349c`. Broader backend tests have 11 identical failures
also reproduced on archived main; those are not silently counted as passing.

## Still required before activation

1. Independent review of the combined authority/journal/native slice and
   exhaustive conflicting-writer inventory (see `compaction-writer-inventory.md`).
2. Integrate the source capture and ingress exclusion into actual summarization
   and runtime entrypoints; close/reopen idle persistent native managers and
   verify all ACP queue/steer paths, not just their canonical store methods.
3. Verified full deployment and trusted bridge origin at every actual runtime
   entrypoint, plus send/publication coupling (the authority lock alone does
   not make split transactions atomic).

Then safe rollout/retirement of old native writers, operator recovery, real
adaptive/ACP integration, and isolated end-to-end tests remain. The independent hard-context
backstop is unchanged. No installed-package edits, provider calls, or live
activation have occurred. Independent review is mandatory before merge.
