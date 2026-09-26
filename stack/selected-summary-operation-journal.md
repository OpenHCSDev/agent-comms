# PR95 selected-summary operation reservation (Python-only, default-OFF)

Status: internal candidate only. No ACP producer calls this reservation yet; no
phase-2 Pi bytes, installed package, provider request, or production opt-in.
The selected idle-child dry-run remains non-authorizing (`routeStatus:
UNVERIFIED_NO_AUTH_RESOLUTION`). Exact Pi phase-1 `2256e2bc…` independently
cleared only its prompt-preflight readiness race; it is not integrated here.

## Durable pre-send boundary

`CompactionJournal.reserve_selected_summary()` extends the existing private
SQLite DELETE/EXTRA, per-transaction post-COMMIT directory-fsync journal. It
records a fresh 128-bit lowercase hex operation ID and bounded JSON containing
source, selected model and settings witnesses **before** any future Pi RPC
write. The partial unique index preserves the valid predecessor schema;
`BEGIN IMMEDIATE` plus a SELECT-any-status guard forbids a **new** attempt on
a session with any historical row. Historical multiple terminal rows are
preserved, never deleted or replayed; exact IDs cannot be reused. There is no
automatic per-session release API. Ambiguous stdin send, process death, timeout or
post-side-effect error stays reserved/UNKNOWN and blocks later provider input
at the existing ACP final-send gate. The journal does not decide whether a
provider was paid; no retry or automatic no-spend inference follows from a
failed transport. The future producer must pass only bounded non-secret
source/model/settings witnesses; this generic journal does not inspect nested
values for credentials or generated summaries, so that producer remains a
review gate.

`CompactionJournal.begin()` refuses a competing native commit while a selected
summary is reserved, except an intent bound to the *same* operation ID. If the
summary is UNKNOWN **or terminal-looking**, all new native commits are refused. The successful
`link_selected_summary_commit()` transition requires that reserved ID in a
same-session **committed** native intent; wrong-session, different-ID,
nonterminal or UNKNOWN attempts cannot link. A post-COMMIT directory-sync
error during bookkeeping may leave a linked row despite the caller receiving
UNKNOWN. A decline transition can likewise leave `declined-prestart` on an
unacknowledged fsync. **Every selected-summary row, including linked and
pre-start declined, remains a durable final-input blocker through restart.**
The owner must not equate a terminal row with successful caller acknowledgement,
provider acceptance, or original-input permission. A correlated, pre-side-effect
Pi decline can be recorded only for `split_turn` or explicit `unsupported`,
but cannot automatically fall back to the original input. Busy, queued, changed
source/model/settings, transport loss and post-auth/stream cancellation remain
operator-blocking; `decline_selected_summary_prestart()` never infers no spend
from silence. The independent native hard-context guard remains unchanged.

## Evidence and remaining integration

Exact `47c8e70` independent review found **NON-CLEAN P2**: both link and
verified pre-start decline could return post-COMMIT fsync UNKNOWN while a
terminal-looking row passed ACP final send in this process and after restart.
See `/dev/shm/pr95-47c8-independent-VfzSGB/REVIEW.md` (SHA256
`3f7c090cae267f326be3895caec054d36279938bebf53a3c1f6e513d94501e00`).
Exact `3ae8e1d` review found the terminal-fsync correction narrowly CLEAN,
but **NON-CLEAN P2 migration/availability**: its all-status unique index
rejects valid predecessor multiple terminal rows, denying unrelated sessions
under that wire root. See `/dev/shm/pr95-3ae8-independent-Yn2gdP/REVIEW.md`
(SHA256 `b994844da3c101c3d7eb713ffe652033449d27920ce8f2f9872944ac183b41d5`).
This successor restores the predecessor partial index, transactionally rejects
all new attempts if any selected row exists, and refuses native begin when
multiple historical rows are present. All historical rows continue to block
their exact session at the ACP gate, without globally denying other sessions.
Tests also exercise same/fresh-process fsync faults, repeated fsync denial,
two-process reservation race and non-destructive migration. Bounded provider-free
serial suites: selected journal **19 passed**; native journal, ACP send barrier
and selected dry-run **26 passed**; Black/Ruff/mypy/diff checks passed (Black
on Python 3.11 warns it cannot AST-verify configured 3.13 grammar). Fresh exact
independent review remains mandatory. Exact `6598755` subsequently received
**scoped CLEAN** for this Python-only migration and fail-closed barrier; see
`/dev/shm/pr95-659-independent-BO57UF/REVIEW.md` (SHA256
`0aeb5d25f9587607cb5ab44cef13dbc40114ea805937d427f3e3330ff7610518`).
This is a *safety backstop*, not an operationally complete selected summary.

Exact `749a4c3` positive admission was independently **NON-CLEAN**: a private
mint helper accepted a persisted terminal row after a post-COMMIT fsync UNKNOWN,
and the transformed sent-text digest did not bind the durable original ingress
`InputDispositions.source_text`. See
`/dev/shm/pr95-749-independent-NqCZ15/REVIEW.md`. The corrective default-OFF
candidate issues a one-use exact terminal ACK receipt **inside the same SQLite
transaction only after its parent fsync returns**; it checks that the selected
row changed from reserved to the exact terminal status in that transaction.
The private mint consumes this non-reconstructible process-local receipt,
never just a row or source JSON. Reservation persists and checks the digest of
the durable original ingress source text separately from the transformed
sent-text digest, and ACP rechecks both at the wire-locked bind. Identity also
binds exact operation/session, owner PID+incarnation, turn, one ingress key,
admission/correction witness, reserved-source revision and fresh native session
revision. A linked token additionally requires the committed native intent to
carry the exact reserved-source JSON digest. ACP's existing final wire-locked send boundary
consumes it at the durable `InputDispositions.bind()` of one native input ID,
before stdin.write. A failed bind, owner/source change, crash or restart loses
it; the journal row remains a general blocker. No ACP producer installs it,
and no token comes from a persisted terminal row or an untrusted Pi summary.
Provider-free fake tests cover successful exact linked/clean-decline ACK,
post-COMMIT fsync UNKNOWN on both terminals with direct row-only mint refusal,
durable original-text mismatch at reservation and after it, mismatched
owner/turn/ingress, correction/text/source drift, committed native source
digest, a forked process, one-use/ABA, post-bind fault, and crash before/after
a local fake stdin write.
Existing ordinary native input remains available without selected attempts.
Bounded serial provider-free corrective checks: selected admission, journal
and send gate **45 passed** (`/var/tmp/pr95-selected-ack-correction-focused.log`,
SHA256 `25a0d94958ba6e522f9727906080fb09fb429d045b7e2b8bb16dac1a8a3c0e23`);
adjacent native journal, dry-run and ACP input/goal regressions **71 passed**
(`/var/tmp/pr95-selected-ack-correction-adjacent.log`, SHA256
`21138219005db534f435fcbbc33300f915bdea864816a026e612321c245e096b`);
Black/Ruff/mypy/diff checks pass (Black under Python 3.11 warns it cannot
AST-verify configured 3.13 grammar). Exact `a6b6974` review's initial scoped
CLEAN characterization was corrected by its reviewer: **NON-CLEAN P1** private
status-only mint, despite successful fsync-UNKNOWN/wrong-original probes
(`/dev/shm/pr95-a6b-independent-4pXfAC/ADDENDUM-status-only-ack.md`, SHA256
`98b466910a3964118d9c6d3d1c81544e22f2ef1d2b1d78d82990b90aeb0c7a17`).
A direct `_transaction(selected_ack=caller_scope)` previously minted after raw
status-only SQL with `decline_reason=NULL`; no public clean-decline method had
run. The corrective successor removes all selected ACK arguments from generic
`_transaction`. Only exact public link or clean-decline methods issue a receipt
after their own checked SQL and returned COMMIT+parent fsync. Direct status-only
SQL now cannot issue a receipt; its terminal row remains a blocker and public
retry refuses. Focused provider-free **49 passed**, including direct linked/
declined status-only denial (`/var/tmp/pr95-selected-closed-terminal-ack-focused.log`,
SHA256 `810e689b7f80b50d6353fdafa801c15030b25069fcf08de64fab96bdd6297c8c`);
adjacent ACP/native-journal/authority/fake guardian **150 passed**
(`/var/tmp/pr95-selected-closed-terminal-ack-adjacent.log`).
Fresh exact independent review is required; no live selected producer, Pi
terminal attestation or original-input success was tested.

Before activating a provider path, the owner must call reserve under current
owner/turn/ingress authority, serialize the **same** exact source/model/settings
into one bounded selected-child request, and never send without durable begin.
Pi phase-2 candidates through `764b69d` independently failed custom
EventStream terminal/slot authority or noncooperative deadline, and may not be
imported or repinned. A separately reviewed exact-child namespace guardian
and model/auth/baseURL/extension parity are required. The separate Linux
guardian prototype exact `9a40d274` received scoped CLEAN only for provider-
free dedicated child and namespace retirement tests (review
`/dev/shm/pr95-guardian-9a-fresh-nnTPLY/REVIEW.md`, archive SHA256
`fbf97797ca75b13970628ef08a803c2275345ffd010554308ebf5141100fe69b`).
Two exact reviewed Python source files and two tests are imported byte-for-byte;
`SelectedSummarySlot.run_selected_summary()` remains unconditionally disabled.
A new fake combined test binds journal operation ID to fake child exchange,
retirement and continued original-input exclusion; 60 guardian/combined plus
selected admission/journal/send tests pass serial/provider-free
(`/var/tmp/pr95-selected-guardian-combined-python.log`, SHA256
`34a9661cace1a171ffb9cb16342cd5a705cd77cfd86391876ab19734bb12081a`).
This is not SDK route proof or a hard 90-second production SLA. Native
provider terminal, child+descendant retire/reap, exact CAS and strict reopen
must precede any live producer minting the positive capability. Current
linked/declined rows still block absent a returned ACK and one-use binding. Native CAS, local metadata, child retirement and reopened-source evidence
must bind the same operation ID. Source/correction/settings/goal changes refuse
the handoff; no UNKNOWN input is replayed. Full provider-free ACP E2E,
wheel/source suites, operator recovery and independent exact PR94-merged
review are still required. The normal merge of main `1273f0f` reconciles ACP
adaptive/N-K constructor kwargs and `_store_lock` bounded-read options with
the inherited child descriptor; focused provider-free serial PR94+PR95 cases
passed **326 with 1 skipped** (`/var/tmp/pr95-pr94-merge-focused.log`),
and adjacent PR94 native-send/disposition/runtime/backend plus PR95 authority
**263 passed** (`/var/tmp/pr95-pr94-merge-adjacent.log`). A later separate
provider-free PR94 private raw-writer seam draft carries the exact Pi
`get_state.sessionFile` into the isolated one-use writer and holds this selected
journal's BEGIN IMMEDIATE through raw `os.write` under PR94 wire→bus→registry→
store locks. Same-session selected reserved/UNKNOWN/terminal rows (even without
a returned ACK) deny that write; other sessions proceed, and direct concurrent
reservation is serialized. Exact-`6096df9` disposable baseline failed three
same-session negative cases before this guard
(`/var/tmp/pr95-private-crossroute-before.log`); corrective source/fake
combined **167 passed, 1 pinned-Pi-import test deliberately deselected**
(`/var/tmp/pr95-private-crossroute-focused.log`) and adjacent ACP/private bus
**148 passed** (`/var/tmp/pr95-private-crossroute-adjacent.log`). This only
closes raw-write AFTER an existing selected row. Selected reservation AFTER
an unresolved PR94 private input is not yet barred by a durable cross-store
proof and remains a **pre-opt-in blocker**, alongside actual Pi transport/route
auth parity. No paid operation or activation is authorized.
